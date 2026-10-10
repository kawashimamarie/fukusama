(function () {
  var loadingStartedAt = Date.now();
  var minimumLoadingTime = 1500;
  var loadingHidden = false;

  var revealSelector = [
    '.section-head',
    '.about-wide',
    '.business-card',
    '.site-card',
    '.brand-feature',
    '.brand-grid article',
    '.product-card',
    '.profile-card',
    '.side-stack article',
    '.timeline li',
    '.group-grid article',
    '.recruit',
    '.contact-grid article',
    '.lead-panel',
    '.visual-band',
    '.content-card',
    '.data-card',
    '.cta-box',
    '.image-area'
  ].join(',');

  function setupRevealAnimations() {
    var elements = Array.prototype.slice.call(document.querySelectorAll(revealSelector));

    elements.forEach(function (element, index) {
      element.classList.add('js-reveal');
      element.style.setProperty('--reveal-delay', Math.min(index % 4, 3) * 0.08 + 's');
    });

    if (!('IntersectionObserver' in window)) {
      elements.forEach(function (element) {
        element.classList.add('is-visible');
      });
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) {
          return;
        }

        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, {
      threshold: 0.16,
      rootMargin: '0px 0px -8% 0px'
    });

    elements.forEach(function (element) {
      observer.observe(element);
    });
  }

  function hideLoadingScreen() {
    if (loadingHidden) {
      return;
    }

    loadingHidden = true;
    var loadingScreen = document.querySelector('.loading-screen');

    document.body.classList.remove('is-loading');
    document.body.classList.add('is-loaded');

    if (!loadingScreen) {
      return;
    }

    loadingScreen.classList.add('is-hidden');
    loadingScreen.setAttribute('aria-hidden', 'true');
  }

  function scheduleLoadingScreenHide() {
    var elapsed = Date.now() - loadingStartedAt;
    var delay = Math.max(minimumLoadingTime - elapsed, 0);

    window.setTimeout(hideLoadingScreen, delay);
  }

  function initPageMotion() {
    if (!document.body) {
      return;
    }

    document.body.classList.add('is-loading');
    setupRevealAnimations();

    if (document.readyState === 'complete') {
      scheduleLoadingScreenHide();
      return;
    }

    window.addEventListener('load', scheduleLoadingScreenHide, { once: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPageMotion, { once: true });
  } else {
    initPageMotion();
  }
})();
