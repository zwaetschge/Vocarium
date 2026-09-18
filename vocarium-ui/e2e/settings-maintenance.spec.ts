import {test,expect} from '@playwright/test';
import {mockApi} from './mock-api';
const profile={provider:'codex',model:'existing',reasoning_effort:'high',timeout_seconds:1200,fallbacks:[]};

test('settings are one tap away at desktop, cover and short window sizes',async({page},info)=>{
 await mockApi(page);
 for(const [width,height] of [[1440,960],[499,789],[360,430]]){
  await page.setViewportSize({width,height});await page.goto('/podcast');
  const link=page.getByRole('link',{name:'Einstellungen öffnen',exact:true});
  await expect(link).toBeVisible();const box=await link.boundingBox();expect(box!.height).toBeGreaterThanOrEqual(44);
  await link.click();await expect(page.getByRole('heading',{name:'Einstellungen',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'CLIs & Modelle',exact:true})).toBeVisible();
  expect(await page.locator('.app-main').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBe(true);
 }
 await page.screenshot({path:`../docs/implementation/settings-maintenance/settings-${info.project.name}.png`});
});

test('refresh adds models without replacing edited profiles',async({page},info)=>{
 await mockApi(page);let refreshed=false;let posts=0;let done=false;
 const base={clis:{codex:{installed:'1.0.0',latest:'1.1.0'},claude:{installed:'2.0.0',latest:'2.0.1'}},catalogs:{codex:{models:[{value:'new',label:'New model'}],source:'CLI',refreshed_at:'2026-09-06T18:00:00Z'}}};
 await page.route('**/api/hoerspiele/settings/agents',route=>route.fulfill({json:{research:profile,scripting:profile,provider_options:[{value:'codex',label:'Codex CLI'}],model_options:{codex:[{value:'existing',label:'Existing'},...(refreshed?[{value:'new',label:'New model'}]:[])]},reasoning_options:[{value:'high',label:'Hoch'}]}}));
 await page.route('**/api/hoerspiele/settings/agents/maintenance**',route=>{
  if(route.request().method()==='POST'){posts++;return route.fulfill({status:202,json:{...base,job:{id:'job',kind:'models',status:'running'}}});}
  if(posts&&done)refreshed=true;
  return route.fulfill({json:{...base,job:posts?{id:'job',kind:'models',status:done?'completed':'running'}:null}});
 });
 await page.goto('/settings/ki');await expect(page.getByRole('button',{name:'Codex CLI aktualisieren',exact:true})).toBeEnabled();
 const timeout=page.locator('input[type=number]').first();await timeout.fill('900');
 await page.getByRole('button',{name:'Modelllisten aktualisieren',exact:true}).click();
 await expect(page.getByRole('button',{name:'Codex CLI aktualisieren',exact:true})).toBeDisabled();
 done=true;await expect(page.locator('option[value=new]').first()).toBeAttached({timeout:10000});
 await expect(timeout).toHaveValue('900');
 await expect(page.locator('select').nth(1)).toHaveValue('existing');expect(posts).toBe(1);
 await page.screenshot({fullPage:true,path:`../docs/implementation/settings-maintenance/models-${info.project.name}.png`});
});

test('a busy production reports a recoverable update error',async({page})=>{
 await mockApi(page);
 await page.route('**/api/hoerspiele/settings/agents',r=>r.fulfill({status:503,json:{detail:'Runner temporarily busy'}}));
 await page.route('**/api/hoerspiele/settings/agents/maintenance**',route=>route.request().method()==='POST'?route.fulfill({status:409,json:{detail:'An audio-drama production is active. Update the CLI after it finishes.'}}):route.fulfill({json:{clis:{},catalogs:{},job:null}}));
 await page.goto('/settings/ki');await page.getByRole('button',{name:'Codex CLI aktualisieren',exact:true}).click();
 await expect(page.getByRole('alert')).toContainText('production is active');
 await expect(page.getByRole('button',{name:'Codex CLI aktualisieren',exact:true})).toBeEnabled();
});

test('saved podcast providers refresh models without saving or replacing selection',async({page})=>{
 await mockApi(page);let writes=0;
 await page.route('**/api/llm/providers',r=>r.fulfill({json:{providers:[{id:'mine',name:'My provider',base_url:'https://example.com/v1',model:'kept',is_active:true,temperature:0.8,max_tokens:16384,provider_type:'openai'}]}}));
 await page.route('**/api/llm/providers/mine/models',r=>{if(r.request().method()!=='GET')writes++;return r.fulfill({json:{models:['new','kept'],refreshed_at:'2026-09-06T19:00:00Z'}});});
 await page.goto('/settings/allgemein');await page.getByRole('button',{name:'Bearbeiten',exact:true}).click();
 await page.getByRole('button',{name:'Modellliste aktualisieren',exact:true}).click();
 await expect(page.getByRole('status')).toContainText('2 Modelle aktualisiert');
 await expect(page.locator('input[list="provider-models"]')).toHaveValue('kept');
 await expect(page.locator('#provider-models option')).toHaveCount(2);expect(writes).toBe(0);
});
