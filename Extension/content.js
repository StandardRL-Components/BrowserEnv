/**
 * content.js
 *
 * Content script for BrowserEnv extension.
 * Captures DOM events (clicks, selections, scroll, hover, mouse movements)
 * and relays structured JSON messages to the background script.
 */

let lastX = 0;
let lastY = 0;
let mouseX = 0;
let mouseY = 0;
let closenessCount = 0;
let chosenLink = null;

/**
 * Handle click events by extracting the target element's text and bounding rectangle,
 * then send a 'click' message to the background script.
 * @param {MouseEvent} event - Click event on the document.
 */
function handleElementClick(event) {
  const element = event.target;
  const text = element.innerText.trim();
  const rect = element.getBoundingClientRect();

  const clickMessage = {
    type: "click",
    text,
    bounding_rect: {
      top: rect.top,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      right: rect.right,
      bottom: rect.bottom
    }
  };

  browser.runtime.sendMessage(clickMessage);
}

/**
 * Determine if a link element is visible within the viewport and not hidden by CSS.
 * @param {HTMLAnchorElement} link - Anchor element to test.
 * @returns {boolean} True if visible and in-viewport.
 */
function isLinkVisible(link) {
  const r = link.getBoundingClientRect();
  const inViewport = (
    r.top >= 0 && r.left >= 0 &&
    r.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
    r.right <= (window.innerWidth || document.documentElement.clientWidth)
  );
  const hasSize = (r.width > 0 && r.height > 0);
  const style = window.getComputedStyle(link);
  const notHidden = (
    style.visibility !== "hidden" &&
    style.opacity !== "0" &&
    style.display !== "none"
  );

  return inViewport && hasSize && notHidden;
}

/**
 * Split selected text into an array of complete sentences, cleaning whitespace and HTML.
 * @param {string} text - Raw selected text.
 * @returns {string[]} Array of trimmed sentences.
 */
function getCompleteSentencesFromSelection(text) {
  const pattern = /([^.!?]*[.!?])/g;
  const matches = text.match(pattern);
  if (!matches) return [];

  return matches
    .map(s => s.replace(/[^ -~]+/g, ' '))
    .map(s => s.replace(/<[^>]+>/g, '').trim())
    .filter(s => s.length > 0);
}

/**
 * Handle text selection and scrolling events.
 * Sends 'highlight', 'scroll', and 'mouseup' messages as appropriate.
 */
function handleTextSelection() {
  const selection = window.getSelection();
  const range = selection.rangeCount > 0 ? selection.getRangeAt(0) : null;

  if (range && !selection.isCollapsed) {
    const rawText = selection.toString().trim();
    const sentences = getCompleteSentencesFromSelection(rawText);
    if (sentences.length > 0) {
      browser.runtime.sendMessage({ type: "highlight", value: sentences });
    }
  }

  const currentX = window.scrollX;
  const currentY = window.scrollY;
  if (currentX !== lastX || currentY !== lastY) {
    browser.runtime.sendMessage({
      type: "scroll",
      value_x: currentX - lastX,
      value_y: currentY - lastY
    });
  }

  lastX = currentX;
  lastY = currentY;

  browser.runtime.sendMessage({ type: "mouseup" });
}

/**
 * Calculate a suggestion vector towards a chosen or random visible link.
 * Maintains state of chosenLink and resets after repeated proximity.
 * @param {number} x - Current mouse X coordinate.
 * @param {number} y - Current mouse Y coordinate.
 * @returns {Object|null} Suggestion with distanceX, distanceY, href, reason.
 */
function getRandomLinkAndMouseDistances(x, y) {
  if (chosenLink && document.body.contains(chosenLink)) {
    const r = chosenLink.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    return { distanceX: x - cx, distanceY: y - cy, href: chosenLink.href, reason: "Existing" };
  }

  const links = Array.from(document.querySelectorAll('a')).filter(isLinkVisible);
  if (links.length === 0) return null;

  chosenLink = links[Math.floor(Math.random() * links.length)];
  const r2 = chosenLink.getBoundingClientRect();
  const cx2 = r2.left + r2.width / 2;
  const cy2 = r2.top + r2.height / 2;

  return { distanceX: x - cx2, distanceY: y - cy2, href: chosenLink.href, reason: "FoundNew" };
}

/**
 * Reset the stored chosen link, forcing a new selection on next suggestion.
 */
function resetLinkChoice() {
  chosenLink = null;
}

/**
 * Enumeration of all hyperlink URLs present in the document.
 * @returns {string[]} Array of href strings.
 */
function getAllLinkUrls() {
  return Array.from(document.querySelectorAll('a'))
    .map(a => a.href.trim())
    .filter(u => u.length > 0);
}

// Register event listeners for user interactions

document.addEventListener('click', handleElementClick);
document.addEventListener('mousedown', () => {
  lastX = window.scrollX;
  lastY = window.scrollY;
  browser.runtime.sendMessage({ type: 'mousedown' });
});
document.addEventListener('mouseup', () => setTimeout(handleTextSelection, 10));
document.addEventListener('mousemove', (event) => {
  mouseX = event.clientX;
  mouseY = event.clientY;

  if (event.target.tagName === 'A') {
    browser.runtime.sendMessage({ type: 'linkhover' });
  }

  const suggestion = getRandomLinkAndMouseDistances(mouseX, mouseY);
  if (suggestion) {
    browser.runtime.sendMessage({ type: 'mousemove', suggestion });
    if (Math.abs(suggestion.distanceX) < 5 && Math.abs(suggestion.distanceY) < 5) {
      closenessCount++;
      if (closenessCount > 6) {
        resetLinkChoice();
        closenessCount = 0;
      }
    } else {
      closenessCount = 0;
    }
  }
});

// Periodically send an undefined message to keep the pipeline alive
setInterval(() => browser.runtime.sendMessage(undefined), 10000);

// On load, dispatch all links for exploration tracking
window.addEventListener('load', () => {
  setTimeout(() => {
    browser.runtime.sendMessage({ type: 'links', value: getAllLinkUrls() });
  }, 200);
});
