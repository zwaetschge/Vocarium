import { chromium, expect } from '@playwright/test';
expect.configure({ timeout: 30000 });
const base=process.env.HS_TEST_URL || 'http://localhost:4307';
const browser=await chromium.launch({executablePath:'/usr/local/bin/plum-chromium',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000},serviceWorkers:'block'});
const writes=[];
let closing=false;
await page.route('**/api/**',async route=>{
 if(closing) { await route.abort(); return; }
 try {
 const req=route.request(); if(req.method()!=='GET'){writes.push(req.method()+' '+req.url()); await route.abort(); return;}
 const url=new URL(req.url());
 const res=await route.fetch({url:'http://vocarium-ui:3000'+url.pathname+url.search,headers:{...req.headers(),'Remote-User':'zwaetschge'}});
 await route.fulfill({response:res});
 } catch(error) { if(!closing) throw error; }
});
await page.goto(base+'/hoerspiele/prj_ab41eb801b6c45ca');
const select=page.getByLabel('Buch wechseln',{exact:true});
await expect(select).toBeVisible();
await expect(select.locator('option')).not.toHaveCount(1);
const options=await select.locator('option').evaluateAll(items=>items.map(i=>({id:i.value,title:i.textContent})));
console.log(options);
await page.screenshot({path:'/mnt/cache/AI/plum-code/voxtral/.work/hs-switch/after-desktop.png'});
const other=options.find(o=>o.id!=='prj_ab41eb801b6c45ca');
await select.selectOption(other.id);
await expect(page.getByRole('heading',{name:other.title,exact:true})).toBeVisible({timeout:30000});
await page.goBack();
await expect(page.getByRole('heading',{name:'Dragon Ball Band 10',exact:true})).toBeVisible();
await page.setViewportSize({width:390,height:844});
await page.screenshot({path:'/mnt/cache/AI/plum-code/voxtral/.work/hs-switch/after-mobile.png'});
if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow');
await page.getByRole('link',{name:'← Alle Hörspiele',exact:true}).click();
await expect(page).toHaveURL(base+'/hoerspiele');
if(writes.length)throw Error('Navigation attempted writes: '+writes.join(','));
console.log('PASS: switch, back, overview, mobile overflow, no mutations');
closing=true;
await browser.close();
