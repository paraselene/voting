const scannedPassword = new URLSearchParams(window.location.hash.slice(1)).get('password');
if (scannedPassword !== null) {
  window.history.replaceState(null, '', window.location.pathname + window.location.search);
  const loginForm = document.querySelector('#login-form');
  if (/^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{5}$/.test(scannedPassword)) {
    if (loginForm) {
      loginForm.querySelector('input[name="password"]').value = scannedPassword;
      loginForm.requestSubmit();
    } else {
      window.location.replace('/?qr=1#password=' + scannedPassword);
    }
  }
}

document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
  form.querySelector('button').disabled = false;
});

const form = document.querySelector('#vote-form');
if (form) {
  const boxes = [...form.querySelectorAll('input[name="candidate"]')];
  const count = document.querySelector('#selection-count');
  const submit = document.querySelector('#submit-vote');
  function update() {
    const total = boxes.filter(box => box.checked).length;
    count.textContent = `已選 ${total} / 10 位`;
    if (submit) {
      submit.disabled = total > 10;
      boxes.forEach(box => { box.disabled = total >= 10 && !box.checked; });
    }
  }
  boxes.forEach(box => box.addEventListener('change', update));
  form.addEventListener('submit', event => {
    if (!boxes.some(box => box.checked) && !window.confirm('你尚未選擇候選人。確定提交空白票？')) event.preventDefault();
  });
  update();
}
