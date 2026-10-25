const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');

const source = readFileSync(`${__dirname}/../election/static/election/vote.js`, 'utf8');

function page(hash = '', hasLoginForm = true) {
  const events = {};
  const input = { value: '' };
  const submissions = [];
  const redirects = [];
  const location = { hash, pathname: '/', search: '' };
  const form = {
    querySelector: () => input,
    requestSubmit: () => {
      assert.equal(location.hash, '', 'Clear the credential before submitting');
      submissions.push(input.value);
    },
  };
  runInNewContext(source, {
    URLSearchParams,
    window: {
      location: Object.assign(location, { replace: url => redirects.push(url) }),
      history: { replaceState: (_, __, url) => {
        assert.equal(url, location.pathname + location.search);
        location.hash = '';
      } },
      addEventListener: (event, handler) => { events[event] = handler; },
    },
    document: {
      querySelector: selector => selector === '#login-form' && hasLoginForm ? form : null,
      querySelectorAll: () => [],
    },
  });
  return { location, submissions, redirects, emit: event => events[event]?.() };
}

test('A newly opened QR link submits once and clears the credential', () => {
  const browser = page('#password=ABCDE');
  browser.emit('pageshow');
  assert.deepEqual(browser.submissions, ['ABCDE']);
});

for (const event of ['hashchange', 'pageshow']) {
  test(`An existing page handles QR credentials on ${event}`, () => {
    const browser = page();
    browser.location.hash = '#password=ABCDE';
    browser.emit(event);
    browser.emit('pageshow');
    assert.deepEqual(browser.submissions, ['ABCDE']);
    browser.location.hash = '#password=23456';
    browser.emit(event);
    assert.deepEqual(browser.submissions, ['ABCDE', '23456']);
  });
}

test('A signed-in page routes a scan through the account-switch login', () => {
  const browser = page('', false);
  browser.location.pathname = '/vote/';
  browser.location.hash = '#password=ABCDE';
  browser.emit('hashchange');
  assert.equal(browser.location.hash, '');
  assert.deepEqual(browser.redirects, ['/?qr=1#password=ABCDE']);
});

test('Invalid credentials are cleared without submitting or redirecting', () => {
  const browser = page();
  for (const password of ['00000', '', 'ABCDEF', '%3Cscript%3E']) {
    browser.location.hash = '#password=' + password;
    browser.emit('hashchange');
    assert.equal(browser.location.hash, '');
  }
  assert.deepEqual(browser.submissions, []);
  assert.deepEqual(browser.redirects, []);
});

function control(properties = {}) {
  return Object.assign({ handlers: {}, addEventListener(event, handler) { this.handlers[event] = handler; } }, properties);
}

for (const maximum of [1, 3, 10, 19]) {
  test(`Voting controls enforce a configured maximum of ${maximum}`, () => {
    const boxes = Array.from({ length: 19 }, () => control({ checked: false, disabled: false }));
    const count = {};
    const submit = {};
    const form = control({ dataset: { maximum: String(maximum) }, querySelectorAll: () => boxes });
    runInNewContext(source, {
      URLSearchParams,
      window: { location: { hash: '' }, addEventListener() {}, confirm: () => true },
      document: { querySelector: selector => ({ '#vote-form': form, '#selection-count': count, '#submit-vote': submit }[selector] || null), querySelectorAll: () => [] },
    });
    assert.equal(count.textContent, `已選 0 / ${maximum} 位`);
    for (let i = 0; i < maximum; i++) boxes[i].checked = true;
    boxes[0].handlers.change();
    assert.equal(count.textContent, `已選 ${maximum} / ${maximum} 位`);
    assert.equal(submit.disabled, false);
    if (maximum < 19) assert.equal(boxes[maximum].disabled, true);
    boxes[0].checked = false;
    boxes[0].handlers.change();
    assert.equal(boxes[18].disabled, false);
  });
}

test('Round controls select all/none, default title limits, and bound the limit to participation', () => {
  const boxes = Array.from({ length: 19 }, () => control({ checked: true }));
  const title = control({ value: '10位執事' });
  const maximum = { value: 10, max: 19 };
  const all = control({ dataset: { candidates: 'all' } });
  const none = control({ dataset: { candidates: 'none' } });
  const form = {
    querySelector: selector => selector === '[name="title"]' ? title : maximum,
    querySelectorAll: selector => selector === '[name="candidate"]' ? boxes : [all, none],
  };
  runInNewContext(source, {
    URLSearchParams,
    window: { location: { hash: '' }, addEventListener() {} },
    document: { querySelector: selector => selector === '#create-round' ? form : null, querySelectorAll: () => [] },
  });
  title.value = '文書'; title.handlers.change();
  assert.equal(maximum.value, 1);
  title.value = '10位執事'; title.handlers.change();
  assert.equal(maximum.value, 10);
  none.handlers.click();
  assert.ok(boxes.every(box => !box.checked));
  assert.equal(maximum.max, 0);
  all.handlers.click();
  assert.ok(boxes.every(box => box.checked));
  assert.equal(maximum.value, 1);
  assert.equal(maximum.max, 19);
  boxes.slice(2).forEach(box => { box.checked = false; });
  title.handlers.change();
  assert.equal(maximum.value, 2);
});
