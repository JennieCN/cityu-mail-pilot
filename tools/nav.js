/**
 * Shared navigation helpers for the browser checks.
 *
 * The app used to be one long page with a `#tabs` strip near the bottom. It is
 * now an app shell: the same registry is projected into a desktop sidebar, a
 * phone tab bar, and a drawer, and the current section lives in the URL hash.
 *
 * So a check must not click a fixed container — which of the three surfaces is
 * on screen depends on the viewport, and on a phone some destinations only
 * exist inside the drawer. These helpers drive the hash instead, which is the
 * real navigation contract, and expose the nav surfaces for the checks that
 * are specifically about the nav.
 */
'use strict';

/** Go to a section the way a user would end up there, at any viewport. */
async function goTo(page, key) {
  await page.evaluate((section) => { window.location.hash = `#/${section}`; }, key);
  await page.waitForFunction(
    (section) => window.location.hash === `#/${section}`, key, { timeout: 10000 });
  if (key === 'dashboard') {
    await page.waitForSelector('#view-dashboard:not(.hidden)', { timeout: 10000 });
  } else {
    await page.waitForSelector(`#section-${key}:not(.hidden)`, { timeout: 10000 });
  }
}

/** True when the account is offered this destination at all (any surface). */
function navHas(page, key) {
  return page.locator(`#sidebar-nav button[data-section="${key}"]`).count();
}

/** The label the shell is currently showing in the top bar. */
function currentTitle(page) {
  return page.innerText('#app-title');
}

/**
 * Open an admin <details> panel.
 *
 * The admin console loads each panel lazily, so its contents do not exist until
 * the panel is opened. A check that waits for a panel's contents without
 * opening it first fails as a visibility timeout, which reads like a broken
 * feature rather than a missing click.
 */
async function openPanel(page, id) {
  const details = page.locator(`#${id}`);
  if (!(await details.count())) return false;
  if (await details.evaluate((node) => node.open)) return true;
  const summary = page.locator(`#${id} > summary`);
  if (!(await summary.count())) return false;
  await summary.click();
  await page.waitForTimeout(250);
  return true;
}

/**
 * Mint a single-use invite with a throwaway operator session.
 *
 * Several checks used to require a pre-seeded pool of codes (`PILOT_INVITE-N`)
 * that had to exist before the run. That precondition failed as "cannot
 * register", which looks exactly like a broken registration flow — and once an
 * invite in the pool was consumed by an earlier run, the failure became
 * permanent. Minting one here removes the fixture entirely.
 */
async function mintInvite(browser, { base, email, password, label }) {
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.goto(`${base}/app`, { waitUntil: 'load' });
    await page.fill('#auth-email', email);
    await page.fill('#auth-password', password);
    await page.click('#login');
    await page.waitForSelector('#dashboard:not(.hidden)', { timeout: 15000 });
    const response = await page.request.post(`${base}/api/admin/invites`, {
      data: { label, days: 1 },
    });
    if (!response.ok()) return '';
    return (await response.json()).code || '';
  } catch (error) {
    return '';
  } finally {
    await context.close();
  }
}

module.exports = { goTo, navHas, currentTitle, openPanel, mintInvite };
