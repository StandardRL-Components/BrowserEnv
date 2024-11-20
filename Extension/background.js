let ws; // WebSocket instance
const baseDomain = "uk02.c.local.do-not-trust.standardrl.com"; // Replace with the desired domain

/**
 * Function to connect to a WebSocket server.
 * @param {string} websocketUrl - The WebSocket server URL.
 */
function connectWebSocket(websocketUrl) {
  if (ws) {
    ws.close(); // Close existing WebSocket connection if any
  }

  ws = new WebSocket(websocketUrl);

  ws.onopen = () => {
    console.log("WebSocket connected:", websocketUrl);
  };

  ws.onmessage = (event) => {
    console.log("Message from server:", event.data);
  };

  ws.onclose = () => {
    console.log("WebSocket connection closed. Reconnecting...");
    setTimeout(() => connectWebSocket(websocketUrl), 5000); // Retry connection after 5 seconds
  };

  ws.onerror = (error) => {
    console.error("WebSocket error:", error);
  };
}

/**
 * Extract the value of a specific query parameter from a URL.
 * @param {string} url - The URL to parse.
 * @param {string} param - The query parameter to extract.
 * @returns {string|null} - The value of the query parameter, or null if not found.
 */
function getQueryParam(url, param) {
  const urlObj = new URL(url);
  return urlObj.searchParams.get(param);
}

browser.runtime.onMessage.addListener((message, sender) => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(message));
    console.log("Message sent to WebSocket:", message);
  } else {
    console.warn("WebSocket not open. Message not sent:", message);
  }
});

// Listen for page navigation events
browser.webNavigation.onCommitted.addListener((details) => {
  const { url, frameId } = details;

  // Only handle top-level navigations
  if (frameId !== 0) return;

  if (ws && ws.readyState === WebSocket.OPEN) {
    const navigationMessage = {
      type: "navigation",
      url: details.url
    };
    ws.send(JSON.stringify(navigationMessage));
    console.log("Navigation event sent:", navigationMessage);
  }else{
    if (url.includes("assets.standardrl.com/redirects/browser")) {
      // Get the value of the 'subnet' query parameter
      const subnet = getQueryParam(url, "subnet");

      if (subnet) {
        if (/^\d+$/.test(subnet)) {
          // Valid subnet, proceed with connection
          // Construct the WebSocket URL using 'subnet' and 'baseDomain'
          const websocketUrl = `wss://${subnet}.${baseDomain}/s`;
          console.log("Target URL detected. Connecting WebSocket to:", websocketUrl);

          // Connect to the WebSocket server
          connectWebSocket(websocketUrl);
        }
      } else {
        console.warn("Target URL detected but 'subnet' parameter is missing.");
      }
    }
  }

});