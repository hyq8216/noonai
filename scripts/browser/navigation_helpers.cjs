// Exercise disclosure controls just as an operator does; do not bypass hidden menus.
async function clickNavigation(page, route) {
  await page.locator('main h1').waitFor();
  const mobileToggle = page.locator('#nav-mobile-toggle');
  if (await mobileToggle.isVisible() && await mobileToggle.getAttribute('aria-expanded') === 'false') {
    await mobileToggle.click();
  }
  const target = page.locator(`.nav [data-nav="${route}"]`);
  await target.waitFor({state:'attached'});
  const parents = await target.evaluate(node => {
    const panels=[];
    for(let parent=node.parentElement;parent;parent=parent.parentElement) {
      if(parent.hidden && parent.id.startsWith('nav-panel-'))panels.push(parent.id);
    }
    return panels.reverse();
  });
  for(const id of parents)await page.locator(`.nav [aria-controls="${id}"]`).click();
  await target.click();
}
module.exports={clickNavigation};
