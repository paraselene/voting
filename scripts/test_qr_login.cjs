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
