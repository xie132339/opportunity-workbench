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
  const collapsePhoneSections = () => {
    if (phone.matches) document.querySelectorAll('[data-mobile-collapse]').forEach(section => section.open = false);
  };
  collapsePhoneSections();
  phone.addEventListener('change', collapsePhoneSections);
});
