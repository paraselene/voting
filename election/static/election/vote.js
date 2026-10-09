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
