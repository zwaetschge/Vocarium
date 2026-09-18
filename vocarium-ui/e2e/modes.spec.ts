import {test,expect} from '@playwright/test';
import {mockApi} from './mock-api';
test('four workflow surfaces',async({page},info)=>{
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
 await mockApi(page);
 await page.emulateMedia({reducedMotion:'reduce'});
 await page.route('**/api/audiobooks',r=>r.fulfill({json:{books:[]}}));
 await page.route('**/api/audiobooks/collections',r=>r.fulfill({json:{collections:[]}}));
 await page.route('**/api/hoerspiele/projects',r=>r.fulfill({json:[]}));
 await page.route('**/api/hoerspiele/pipeline-runs?*',r=>r.fulfill({json:[]}));
 await page.addInitScript(()=>localStorage.setItem('ab-onboarding-done','1'));
 for(const [name,path] of [['books','/audiobooks'],['podcasts','/podcast'],['plays','/hoerspiele'],['speech','/']]) {
  await page.goto(path);
  await expect(page.locator('h1').first()).toBeVisible();
  await expect(page.locator('nav[aria-label="Hauptmodi"]:visible a')).toHaveCount(4);
  await page.screenshot({path:`../docs/implementation/four-workflows/after-${name}-${info.project.name}.png`});
 }
});

test('all four modes remain available on cover, inner and short windows',async({page},info)=>{
 test.skip(info.project.name!=='chromium-desktop');test.setTimeout(120000);
 await mockApi(page);
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/hoerspiele/projects',r=>r.fulfill({json:[]}));
 await page.route('**/api/hoerspiele/pipeline-runs?*',r=>r.fulfill({json:[]}));
 for(const [name,width,height] of [['cover',499,789],['inner',979,739],['inner-portrait',739,979],['small-window',360,430]] as const){
  await page.setViewportSize({width,height});await page.goto('/hoerspiele');
  const modes=page.locator('nav[aria-label="Hauptmodi"]:visible');
  await expect(modes.getByRole('link')).toHaveCount(4);
  await expect(modes.getByRole('link',{name:/^Hörspiele/})).toHaveAttribute('aria-current','page');
  expect(await page.locator('.app-main').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBe(true);
  for(const link of await modes.getByRole('link').all()) {const box=await link.boundingBox();expect(box!.height).toBeGreaterThanOrEqual(44);expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(width+1);}
  await page.screenshot({animations:'disabled',path:`../docs/implementation/four-workflows/fold-${name}.png`});
 }
 expect(errors).toEqual([]);
});
