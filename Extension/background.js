/**
 * background.js
 *
 * Core extension background script for BrowserEnv.
 * Manages WebSocket connectivity, message queuing, and tab lifecycle.
 * Provides reliable IPC between the browser and the Python environment.
 */

let ws = null;
let messageQueue = [];
const baseDomain = "uk02.c.local.do-not-trust.standardrl.com";
let lastTabId = null;

/**
 * Establishes or re-establishes a WebSocket connection.
 * @param {string} websocketUrl - The URL of the WebSocket server.
 * @param {string} reason - Contextual reason for the connection attempt.
 */
function connectWebSocket(websocketUrl, reason) {
  if (ws) {
    ws.close();
  }

  browser.storage.local.set({ websocketUrl });
  ws = new WebSocket(websocketUrl);

  ws.onopen = () => {
    console.log("WebSocket connected:", websocketUrl);
    ws.send(JSON.stringify({ type: "connection", reason }));

    while (messageQueue.length > 0) {
      const msg = messageQueue.shift();
      ws.send(JSON.stringify(msg));
    }
  };

  ws.onmessage = (event) => {
    try {
      const message = JSON.parse(event.data);
      console.log("Message from server:", message);

      if (message.type === "navigation" && message.url) {
        browser.tabs.query({ active: true, currentWindow: true })
          .then((tabs) => {
            if (tabs.length > 0) {
              browser.tabs.create({ url: message.url })
                .then((newTab) => closePreviousTab(newTab.id));
              console.log("Navigated to:", message.url);
            }
          });
      }
    } catch (err) {
      console.error("Error parsing WebSocket message:", err);
    }
  };

  ws.onclose = () => {};
  ws.onerror = (error) => console.error("WebSocket error:", error);
}

/**
 * Extracts the value of a given query parameter from a URL string.
 * @param {string} url - The full URL to parse.
 * @param {string} param - The query parameter key to retrieve.
 * @returns {string|null} The value if present, otherwise null.
 */
function getQueryParam(url, param) {
  const urlObj = new URL(url);
  return urlObj.searchParams.get(param);
}

/**
 * Retrieves the last stored WebSocket URL and attempts reconnection.
 * @param {string} reason - Contextual reason for reconnection.
 */
function reconnectWebSocket(reason) {
  browser.storage.local.get("websocketUrl").then((result) => {
    if (result.websocketUrl) {
      connectWebSocket(result.websocketUrl, reason);
    } else {
      console.warn("No WebSocket URL found in local storage.");
    }
  });
}

/**
 * Queues or sends a message over the WebSocket.
 * @param {Object} message - The JSON-serializable payload to transmit.
 */
function sendMessageToWebSocket(message) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(message));
    console.log("Message sent:", message);
  } else {
    console.log("WebSocket not connected. Queuing message:", message);
    messageQueue.push(message);
    if (!ws || ws.readyState === WebSocket.CLOSED) {
      reconnectWebSocket("Failed send; socket closed.");
    }
  }
}

/**
 * Closes the previously active tab, if different from the current.
 * @param {number} newTabId - The ID of the newly opened tab.
 */
function closePreviousTab(newTabId) {
  if (lastTabId !== null && lastTabId !== newTabId) {
    browser.tabs.remove(lastTabId).catch((err) => console.error("Error closing tab:", err));
  }
  lastTabId = newTabId;
}

// Listen for messages from content scripts and forward them
browser.runtime.onMessage.addListener((message) => {
  if (message !== undefined) sendMessageToWebSocket(message);
});

// Automatically close old tabs when new tabs are created
browser.tabs.onCreated.addListener((newTab) => closePreviousTab(newTab.id));

// Attempt to reconnect on extension startup
browser.runtime.onStartup.addListener(() => reconnectWebSocket("Extension startup"));

// Handle top-level navigation events and trigger WebSocket connection if needed
browser.webNavigation.onCommitted.addListener((details) => {
  const { url, frameId, tabId } = details;
  if (frameId !== 0) return;

  const navigationMessage = { type: "navigation", url };
  sendMessageToWebSocket(navigationMessage);
  console.log("Navigation event sent:", navigationMessage);

  if (!(ws && ws.readyState === WebSocket.OPEN)) {
    if (url.includes("assets.standardrl.com/redirects/browser")) {
      const subnet = getQueryParam(url, "subnet");
      if (subnet && /^\d+$/.test(subnet)) {
        const wsUrl = `wss://${subnet}.${baseDomain}/${subnet}`;
        console.log("Connecting WebSocket to:", wsUrl);
        connectWebSocket(wsUrl, "Subnet extracted");
      } else {
        console.warn("Missing or invalid 'subnet' parameter in URL.");
      }
    }
  }

  closePreviousTab(tabId);
});
