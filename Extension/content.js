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