document.addEventListener('DOMContentLoaded', () => {
  const phone = window.matchMedia('(max-width: 760px)');
  const prepareTables = () => {
    document.querySelectorAll('.table-wrap table:not(.offer-list-table):not(.source-table)').forEach(table => {
      const header = table.querySelector('thead tr') || [...table.rows].find(row => row.querySelector('th'));
      if (!header) return;
      const labels = [...header.cells].map(cell => cell.textContent.trim().replace(/\s+/g, ' '));
      header.classList.add('mobile-table-head');
      table.classList.add('mobile-data-table');
      table.closest('.table-wrap').classList.add('mobile-card-wrap');
      [...table.rows].filter(row => row !== header).forEach(row => {
        [...row.cells].forEach((cell, index) => {
          if (!cell.dataset.label) cell.dataset.label = labels[index] || '';
        });
      });
    });
  };
  prepareTables();
  const preparePromotionLinks = () => {
    document.querySelectorAll('a[data-app-promotion]').forEach(link => {
      const webUrl = link.dataset.webUrl || link.href;
      const parsed = new URL(webUrl);
      if (parsed.protocol !== 'https:' || parsed.hostname !== 's.click.taobao.com') return;
      link.dataset.webUrl = webUrl;
      link.dataset.originalLabel ||= link.textContent;
      if (phone.matches) {
        link.href = 'tbopen://m.taobao.com/tbopen/index.html?action=ali.open.nav&module=h5&h5Url=' + encodeURIComponent(webUrl);
        link.removeAttribute('target');
        link.textContent = '在淘宝 App 打开 ↗';
        if (!link.nextElementSibling?.classList.contains('affiliate-web-fallback')) {
          const fallback = document.createElement('a');
          fallback.className = 'affiliate-web-fallback';
          fallback.href = webUrl;
          fallback.target = '_blank';
          fallback.rel = 'noopener noreferrer';
          fallback.textContent = '网页备用';
          link.insertAdjacentElement('afterend', fallback);
        }
      } else {
        link.href = webUrl;
        link.target = '_blank';
        link.textContent = link.dataset.originalLabel;
        link.nextElementSibling?.classList.contains('affiliate-web-fallback') && link.nextElementSibling.remove();
      }
    });
  };
  preparePromotionLinks();
  const collapsePhoneSections = () => {
    if (phone.matches) document.querySelectorAll('[data-mobile-collapse]').forEach(section => section.open = false);
  };
  collapsePhoneSections();
  phone.addEventListener('change', () => { collapsePhoneSections(); preparePromotionLinks(); });
});
