// Custom JavaScript for Speakr documentation

document.addEventListener('DOMContentLoaded', function() {
  // Add smooth scrolling for anchor links
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
      const href = this.getAttribute('href');
      if (href === '#') return;
      
      e.preventDefault();
      const target = document.querySelector(href);
      if (target) {
        target.scrollIntoView({
          behavior: 'smooth',
          block: 'start'
        });
        
        // Update URL without jumping
        history.pushState(null, null, href);
      }
    });
  });
  
  // Add external link indicators
  document.querySelectorAll('a[href^="http"]').forEach(link => {
    if (!link.hostname.includes(window.location.hostname)) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
      
      // Add external icon if not already present
      if (!link.querySelector('.external-icon')) {
        const icon = document.createElement('span');
        icon.className = 'external-icon';
        icon.innerHTML = ' ↗';
        icon.style.fontSize = '0.75em';
        icon.style.verticalAlign = 'super';
        link.appendChild(icon);
      }
    }
  });
  
  // Enhance search functionality
  const searchInput = document.querySelector('.md-search__input');
  if (searchInput) {
    // Add keyboard shortcut hint
    searchInput.setAttribute('placeholder', 'Search (Press "/" to focus)');
    
    // Add "/" keyboard shortcut
    document.addEventListener('keydown', function(e) {
      if (e.key === '/' && !searchInput.matches(':focus')) {
        e.preventDefault();
        searchInput.focus();
      }
    });
  }
  
  // Add responsive tables wrapper
  document.querySelectorAll('table').forEach(table => {
    if (!table.parentElement.classList.contains('table-wrapper')) {
      const wrapper = document.createElement('div');
      wrapper.className = 'table-wrapper';
      wrapper.style.overflowX = 'auto';
      table.parentElement.insertBefore(wrapper, table);
      wrapper.appendChild(table);
    }
  });
  
  // Add image zoom functionality
  document.querySelectorAll('.md-content img').forEach(img => {
    img.style.cursor = 'zoom-in';
    img.addEventListener('click', function() {
      const overlay = document.createElement('div');
      overlay.style.position = 'fixed';
      overlay.style.top = '0';
      overlay.style.left = '0';
      overlay.style.width = '100%';
      overlay.style.height = '100%';
      overlay.style.background = 'rgba(0, 0, 0, 0.9)';
      overlay.style.zIndex = '9999';
      overlay.style.display = 'flex';
      overlay.style.alignItems = 'center';
      overlay.style.justifyContent = 'center';
      overlay.style.cursor = 'zoom-out';
      
      const zoomedImg = document.createElement('img');
      zoomedImg.src = img.src;
      zoomedImg.style.maxWidth = '90%';
      zoomedImg.style.maxHeight = '90%';
      zoomedImg.style.objectFit = 'contain';
      
      overlay.appendChild(zoomedImg);
      document.body.appendChild(overlay);
      
      overlay.addEventListener('click', function() {
        document.body.removeChild(overlay);
      });
      
      // Close on Escape key
      document.addEventListener('keydown', function closeOnEscape(e) {
        if (e.key === 'Escape' && document.body.contains(overlay)) {
          document.body.removeChild(overlay);
          document.removeEventListener('keydown', closeOnEscape);
        }
      });
    });
  });
});

// Add loading indicator for slow operations
window.addEventListener('load', function() {
  // Remove any loading indicators
  const loader = document.querySelector('.page-loader');
  if (loader) {
    loader.style.display = 'none';
  }
});

// Print-friendly styling
window.addEventListener('beforeprint', function() {
  document.body.classList.add('print-mode');
});

window.addEventListener('afterprint', function() {
  document.body.classList.remove('print-mode');
});