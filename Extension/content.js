function handleClick(event) {
    const clickedElement = event.target;
  
    const clickMessage = {
      type: "click",
      object: clickedElement.style.cssText || "No style information"
    };
  
    // Send the message to the background script
    browser.runtime.sendMessage(clickMessage);
  }
  
  // Attach the click event listener to all elements on the page
  function attachClickListeners() {
    document.querySelectorAll("*").forEach((element) => {
      element.addEventListener("click", handleClick);
    });
  }
  
  // Attach listeners after the page loads
  window.addEventListener("load", () => {
    attachClickListeners();
  });

  document.addEventListener("mouseup", () => {
    setTimeout(handleTextSelection, 10); // Small delay to ensure selection is complete
  });

  function handleTextSelection() {
    const selection = window.getSelection();
    const range = selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
  
    if (range && !selection.isCollapsed) {
      // Extract plain text and HTML content
      const value = selection.toString().trim();
      const valueHtml = range.cloneContents();
  
      // Convert the HTML content to a string
      const container = document.createElement("div");
      container.appendChild(valueHtml);
  
      // Send the highlight message
      const highlightMessage = {
        type: "highlight",
        value: value,
        value_html: container.innerHTML,
      };
  
      browser.runtime.sendMessage(highlightMessage);
    }
  }