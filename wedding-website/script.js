/* =============================================
   Wedding Website — Interactive JS
   Sam & Molly, October 4, 2026
   ============================================= */

// ── Page Loader ─────────────────────────────
window.addEventListener('load', () => {
  setTimeout(() => {
    document.getElementById('loader').classList.add('hidden');
    document.body.classList.remove('loading');
    // Trigger hero reveals after load
    document.querySelectorAll('.hero .reveal-fade, .hero .reveal-up').forEach((el, i) => {
      setTimeout(() => el.classList.add('revealed'), 200 + i * 120);
    });
    initPetals();
  }, 1500);
});

// ── Navigation ───────────────────────────────
const nav = document.getElementById('nav');
const scrollHint = document.getElementById('scroll-hint');

window.addEventListener('scroll', () => {
  const y = window.scrollY;
  nav.classList.toggle('scrolled', y > 60);
  scrollHint.classList.toggle('hidden', y > 80);
  updateParallax(y);
  updateActiveNav(y);
}, { passive: true });

function updateActiveNav(y) {
  const sections = ['our-story','details','gallery','travel','registry','rsvp'];
  let current = '';
  sections.forEach(id => {
    const el = document.getElementById(id);
    if (el && el.offsetTop - 120 <= y) current = id;
  });
  document.querySelectorAll('.nav-link').forEach(a => {
    a.classList.toggle('active', a.getAttribute('href') === `#${current}`);
  });
}

// ── Mobile Menu ──────────────────────────────
const hamburger = document.getElementById('hamburger');
const mobileMenu = document.getElementById('mobile-menu');
const mobileOverlay = document.getElementById('mobile-overlay');
const mobileClose = document.getElementById('mobile-close');

function openMenu() {
  mobileMenu.classList.add('open');
  mobileOverlay.classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeMenu() {
  mobileMenu.classList.remove('open');
  mobileOverlay.classList.remove('open');
  document.body.style.overflow = '';
}

hamburger.addEventListener('click', openMenu);
mobileClose.addEventListener('click', closeMenu);
mobileOverlay.addEventListener('click', closeMenu);
mobileMenu.querySelectorAll('a').forEach(a => a.addEventListener('click', closeMenu));

// ── Parallax ─────────────────────────────────
const parallaxBg   = document.getElementById('parallax-bg');
const parallaxMid  = document.getElementById('parallax-mid');
const parallaxFront = document.getElementById('parallax-front');

function updateParallax(y) {
  if (y > window.innerHeight) return;
  const pct = y / window.innerHeight;
  parallaxBg.style.transform    = `translateY(${y * 0.45}px)`;
  parallaxMid.style.transform   = `translateY(${y * 0.28}px)`;
  parallaxFront.style.transform = `translateY(${y * 0.12}px)`;
}

// ── Falling Petals ────────────────────────────
function initPetals() {
  const canvas = document.getElementById('petals');
  const ctx = canvas.getContext('2d');
  let W, H;

  function resize() {
    W = canvas.width  = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }
  resize();
  window.addEventListener('resize', resize);

  const COLORS = ['#e8d0da','#d4b0c0','#c9a0b0','#f0e4ea','#ddc5d0'];
  const PETALS_COUNT = 22;

  class Petal {
    constructor(fresh) {
      this.reset(fresh);
    }
    reset(fresh) {
      this.x = Math.random() * W;
      this.y = fresh ? -20 - Math.random() * 200 : Math.random() * H;
      this.size = 5 + Math.random() * 8;
      this.speed = 0.6 + Math.random() * 1.0;
      this.drift = (Math.random() - 0.5) * 0.6;
      this.angle = Math.random() * Math.PI * 2;
      this.spin  = (Math.random() - 0.5) * 0.04;
      this.opacity = 0.3 + Math.random() * 0.45;
      this.color = COLORS[Math.floor(Math.random() * COLORS.length)];
      this.wobble = Math.random() * Math.PI * 2;
      this.wobbleSpeed = 0.02 + Math.random() * 0.02;
    }
    update() {
      this.wobble += this.wobbleSpeed;
      this.x += this.drift + Math.sin(this.wobble) * 0.5;
      this.y += this.speed;
      this.angle += this.spin;
      if (this.y > H + 30) this.reset(true);
    }
    draw() {
      ctx.save();
      ctx.globalAlpha = this.opacity;
      ctx.translate(this.x, this.y);
      ctx.rotate(this.angle);
      ctx.fillStyle = this.color;
      ctx.beginPath();
      ctx.ellipse(0, 0, this.size * 0.6, this.size, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }

  const petals = Array.from({ length: PETALS_COUNT }, () => new Petal(false));
  let animId;

  function loop() {
    ctx.clearRect(0, 0, W, H);
    petals.forEach(p => { p.update(); p.draw(); });
    animId = requestAnimationFrame(loop);
  }

  // Only run petals while hero is visible
  const heroObs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) { loop(); }
      else { cancelAnimationFrame(animId); ctx.clearRect(0, 0, W, H); }
    });
  }, { threshold: 0.1 });
  heroObs.observe(document.getElementById('hero'));
}

// ── Countdown ────────────────────────────────
const weddingDate = new Date('2026-10-04T16:00:00-05:00');
let prevSeconds = null;

function updateCountdown() {
  const diff = weddingDate - new Date();
  if (diff <= 0) {
    ['days','hours','minutes','seconds'].forEach(id => {
      document.getElementById(id).textContent = '0';
    });
    return;
  }

  const days    = Math.floor(diff / 86400000);
  const hours   = Math.floor((diff % 86400000) / 3600000);
  const minutes = Math.floor((diff % 3600000) / 60000);
  const seconds = Math.floor((diff % 60000) / 1000);

  document.getElementById('days').textContent    = days;
  document.getElementById('hours').textContent   = String(hours).padStart(2,'0');
  document.getElementById('minutes').textContent = String(minutes).padStart(2,'0');

  const secEl = document.getElementById('seconds');
  secEl.textContent = String(seconds).padStart(2,'0');
  if (seconds !== prevSeconds) {
    secEl.classList.remove('tick');
    requestAnimationFrame(() => secEl.classList.add('tick'));
    setTimeout(() => secEl.classList.remove('tick'), 200);
    prevSeconds = seconds;
  }
}

updateCountdown();
setInterval(updateCountdown, 1000);

// ── Scroll Reveal ────────────────────────────
const revealObs = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('revealed');
      revealObs.unobserve(entry.target);
    }
  });
}, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });

document.querySelectorAll(
  '.reveal-fade, .reveal-up, .reveal-left, .reveal-right'
).forEach(el => {
  // Don't observe hero elements (handled separately)
  if (!el.closest('.hero')) revealObs.observe(el);
});

// ── Gallery Lightbox ─────────────────────────
const lightbox        = document.getElementById('lightbox');
const lightboxClose   = document.getElementById('lightbox-close');
const lightboxOverlay = document.getElementById('lightbox-overlay');
const lightboxPrev    = document.getElementById('lightbox-prev');
const lightboxNext    = document.getElementById('lightbox-next');
const lightboxImgWrap = document.getElementById('lightbox-img-wrap');
const lightboxCaption = document.getElementById('lightbox-caption');

const galleryItems = [...document.querySelectorAll('.gallery-item')];
const galleryCaptions = [
  'Together from the start',
  'A candid moment',
  'Adventures we shared',
  'Landscapes that moved us',
  'The day you said yes',
  'Our favorite frame',
];

let currentIndex = 0;

function openLightbox(index) {
  currentIndex = index;
  renderLightboxItem(index);
  lightbox.classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeLightbox() {
  lightbox.classList.remove('open');
  document.body.style.overflow = '';
}

function renderLightboxItem(index) {
  const item = galleryItems[index];
  const img = item.querySelector('img');
  lightboxImgWrap.innerHTML = '';
  lightboxCaption.textContent = galleryCaptions[index] || '';

  if (img) {
    const clone = img.cloneNode();
    lightboxImgWrap.appendChild(clone);
  } else {
    // Placeholder — show a styled version of it
    const placeholder = item.querySelector('.placeholder-inner').cloneNode(true);
    lightboxImgWrap.style.background = 'var(--cream-dark)';
    lightboxImgWrap.appendChild(placeholder);
  }
}

galleryItems.forEach((item, i) => {
  item.addEventListener('click', () => openLightbox(i));
  item.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openLightbox(i); }
  });
});

lightboxClose.addEventListener('click', closeLightbox);
lightboxOverlay.addEventListener('click', closeLightbox);

lightboxPrev.addEventListener('click', () => {
  currentIndex = (currentIndex - 1 + galleryItems.length) % galleryItems.length;
  renderLightboxItem(currentIndex);
});
lightboxNext.addEventListener('click', () => {
  currentIndex = (currentIndex + 1) % galleryItems.length;
  renderLightboxItem(currentIndex);
});

document.addEventListener('keydown', e => {
  if (!lightbox.classList.contains('open')) return;
  if (e.key === 'Escape') closeLightbox();
  if (e.key === 'ArrowLeft') { currentIndex = (currentIndex - 1 + galleryItems.length) % galleryItems.length; renderLightboxItem(currentIndex); }
  if (e.key === 'ArrowRight') { currentIndex = (currentIndex + 1) % galleryItems.length; renderLightboxItem(currentIndex); }
});

// ── RSVP Form ────────────────────────────────
function handleRsvp(e) {
  e.preventDefault();
  const form = document.getElementById('rsvp-form');
  form.style.display = 'none';
  document.getElementById('rsvp-success').classList.add('visible');
}

// Attendance radio — hide meal prefs on decline
document.querySelectorAll('input[name="attendance"]').forEach(radio => {
  radio.addEventListener('change', () => {
    const mealGroup = document.getElementById('meal-group');
    mealGroup.style.opacity = radio.value === 'yes' ? '1' : '0.4';
    mealGroup.style.pointerEvents = radio.value === 'yes' ? 'auto' : 'none';
  });
});

// ── Smooth scroll for anchor links ───────────
document.querySelectorAll('a[href^="#"]').forEach(a => {
  a.addEventListener('click', e => {
    const target = document.querySelector(a.getAttribute('href'));
    if (!target) return;
    e.preventDefault();
    const offset = 70;
    const top = target.getBoundingClientRect().top + window.scrollY - offset;
    window.scrollTo({ top, behavior: 'smooth' });
  });
});
