import { expect, test, type Page } from '@playwright/test';
import { mockApi } from './mock-api';
const stamp='2026-09-05T10:00:00Z';
const host={id:'host-1',name:'Mira',voice_id:'omni-1',role:'host',personality:'Klar',speaking_style:'Ruhig',created_at:stamp,updated_at:stamp};
const segment={id:'seg-1',speaker:'Mira',speaker_id:'host-1',text:'Ein erster gesprochener Text.',type:'speech',position:0,word_count:5,estimated_duration:2,voice:'omni-1'};
const podcast={id:'pod-1',topic:'Eine Episode',format:'dialog',duration:'short',language:'de',status:'script_ready',hosts:[host],sources:[],script:{segments:[segment],total_words:5,estimated_duration:2},audio_path:'/audio.wav',audio_duration:30,audio_format:'wav',audio_size:100,audio_sha256:'abc',script_revision:'new',audio_revision:'old',audio_stale:true,created_at:stamp,updated_at:stamp};
const book={id:'abcdef12',title:'Ein gemeinsames Werk',author:'Autorin',format:'txt',voice_id:'omni-1',total_chapters:2,has_cover:false,created_at:stamp,progress:{chapterIndex:1,segmentIndex:1,completed:false,updatedAt:stamp},chapters:[{index:0,title:'Anfang',totalSegments:1,cachedSegments:1,complete:true},{index:1,title:'Schluss',totalSegments:1,cachedSegments:1,complete:true}],generation:null};
const cue={id:'cue_1234567890abcdef',anchor_episode_id:'episode-1',chapter_title:'Anfang',text:'Eine Erzählpassage.',confidence:1,estimated_duration_ms:3000,placement_policy:'insert',timeline_start_sample:96000};
const project={id:'prj-1',title:'Ein gemeinsames Werk',status:'ready',stage:7,updated_at:stamp,source:{filename:'quelle.txt'},chapters:[],cues:[cue],transcript:[],mapping:[{id:'map-1',episode_id:'episode-1',chapter_title:'Anfang',episode:1,confidence:1,evidence:'Beleg'}],binding:{title:'Serie'},warnings:[],artifacts:[{id:'art-1',filename:'audio.m4a',role:'delivery',bytes:100,sha256:'abc'}],timeline:{revision:1,duration_samples:480000,validation:{status:'passed'}},quality_report:{status:'blocked',release_ready:false,score:.5,passed_checks:1,total_checks:2,summary:'Eine offene Prüfung',checks:[{id:'length',label:'Länge',status:'failed',detail:'Bitte '+cue.id+' prüfen.'},{id:'ok',label:'Lautheit',status:'passed',detail:'Stimmt.'}]}};
function wav() { const data=Buffer.alloc(44+8000*2*30); data.write('RIFF');data.writeUInt32LE(data.length-8,4);data.write('WAVEfmt ',8);data.writeUInt32LE(16,16);data.writeUInt16LE(1,20);data.writeUInt16LE(1,22);data.writeUInt32LE(8000,24);data.writeUInt32LE(16000,28);data.writeUInt16LE(2,32);data.writeUInt16LE(16,34);data.write('data',36);data.writeUInt32LE(data.length-44,40);return data; }
async function fixture(page:Page, busy=false) {
 await mockApi(page); await page.addInitScript(()=>localStorage.setItem('ab-onboarding-done','1'));
 const writes:string[]=[]; let links:{book_id:string;project_id:string}[]=[]; const pod=structuredClone(podcast); if(busy) pod.status='generating_audio'; const play=structuredClone(project);
 await page.route('**/api/**',async route=>{
  const url=new URL(route.request().url()); const path=url.pathname; const method=route.request().method();
  const json=(body:unknown,status=200)=>route.fulfill({contentType:'application/json',status,body:JSON.stringify(body)});
  if(!['GET','HEAD'].includes(method)) writes.push(method+' '+path);
  if(path.includes('/audio/stream') || path.includes('/audio-live/') || path.includes('/artifacts/')) return route.fulfill({contentType:'audio/wav',body:wav()});
  if(path==='/api/voices') return json({voices:[{id:'omni-1',name:'Mira lange Stimme',language:'German',source:'omnivoice',has_audio:true}]});
  if(path==='/api/hosts') return json({hosts:[host]});
  if(path==='/api/podcasts') return json({podcasts:[pod]});
  if(path==='/api/podcasts/pod-1') return json(pod);
  if(path==='/api/podcasts/pod-1/sources') return json({sources:[]});
  if(path==='/api/podcasts/tags') return json({tags:[]});
  if(path==='/api/podcasts/pod-1/script/segments/seg-1') { Object.assign(pod.script.segments[0],route.request().postDataJSON()); return json(pod); }
  if(path==='/api/audiobooks') return json({books:[book]});
  if(path==='/api/audiobooks/abcdef12') return json(book);
  if(path==='/api/audiobooks/abcdef12/content') return json({segments:[{index:Number(url.searchParams.get('chapter')||0),chapterIndex:Number(url.searchParams.get('chapter')||0),text:url.searchParams.get('chapter')==='1'?'Gespeicherte letzte Stelle':'Erste Stelle',paragraphBreak:false}]});
  if(path.endsWith('/bookmarks')) return json({bookmarks:[]});
  if(path.endsWith('/offline-voices')) return json({voices:[]});
  if(path==='/api/audiobooks/collections') return json({collections:[]});
  if(path==='/api/library/links') { if(method==='POST') links.push(route.request().postDataJSON()); return json({links}); }
  if(path==='/api/hoerspiele/projects') return json([play]);
  if(path==='/api/hoerspiele/projects/prj-1') return json(play);
  if(path.endsWith('/quality-repair-scope')) return json({cue_ids:[cue.id],locked_count:0,adds_coverage:false,available:true,sample_rate:48000});
  if(path==='/api/hoerspiele/pipeline-runs') return json([]);
  if(path==='/api/hoerspiele/tts/voices') return json({items:[]});
  if(path.includes('/cues/')) { Object.assign(play.cues[0],route.request().postDataJSON()); return json(play.cues[0]); }
  return route.fallback();
 });
 return writes;
}
test('draft survives tabs and filtering, then saves explicitly',async({page})=>{
 const writes=await fixture(page); await page.goto('/podcast'); await page.getByRole('button',{name:/^Skript/}).click();
 await page.getByRole('button',{name:'Bearbeiten',exact:true}).click(); await page.getByLabel('Gesprochener Text').fill('Ein sicherer Entwurf');
 await page.getByRole('button',{name:/^Quellen/}).click(); await page.getByRole('button',{name:/^Skript/}).click();
 await expect(page.getByText('Ungespeicherter Entwurf · zum Fortsetzen bearbeiten')).toBeVisible();
 await page.getByLabel('Skriptsuche').fill('kein-treffer');
 await page.getByLabel('Skriptsuche').fill('');
 await page.getByRole('button',{name:'Bearbeiten',exact:true}).click(); await expect(page.getByLabel('Gesprochener Text')).toHaveValue('Ein sicherer Entwurf');
 expect(writes.some(w=>w.includes('/script/segments'))).toBeFalsy(); await page.getByRole('button',{name:'Speichern',exact:true}).click(); await expect(page.getByText('Ein sicherer Entwurf',{exact:true})).toBeVisible();
});
test('server job is restored while old audio remains accessible',async({page})=>{
 const writes=await fixture(page,true); await page.goto('/podcast'); await page.getByRole('button',{name:/^Audio/}).click();
 await expect(page.getByRole('button',{name:'Neu rendern'})).toBeDisabled(); await expect(page.getByRole('button',{name:'Anhören',exact:true})).toBeVisible();
 expect(writes.filter(w=>w.includes('/generate'))).toEqual([]);
});
test('dialog contains focus and restores it after Escape',async({page})=>{
 await fixture(page); await page.goto('/podcast'); const open=page.getByRole('button',{name:'Neuer Podcast',exact:true}); await open.click();
 const dialog=page.getByRole('dialog',{name:'Neuer Podcast'}); await expect(dialog).toBeVisible();
 await dialog.getByLabel('Thema',{exact:true}).fill('Test');
 for(let i=0;i<18;i++){await page.keyboard.press('Tab');expect(await dialog.evaluate(el=>el.contains(document.activeElement))).toBeTruthy();}
 await page.keyboard.press('Escape');await expect(dialog).toBeHidden(); await expect(open).toBeFocused();
});
test('reader opens saved chapter and retains DOM on navigation and resizing',async({page})=>{
 await fixture(page);await page.goto('/audiobooks/abcdef12');await expect(page.getByText('Gespeicherte letzte Stelle')).toBeVisible();
 await page.locator('.reader-scroll').evaluate(el=>{(window as unknown as {savedReader:Element}).savedReader=el;});
 await page.setViewportSize({width:979,height:739});expect(await page.locator('.reader-scroll').evaluate(el=>(window as unknown as {savedReader:Element}).savedReader===el)).toBeTruthy();
 await page.locator('.reader-workspace a[href="/audiobooks"]').first().click(); await expect(page.getByText('Fertig',{exact:true})).toHaveCount(0);
 await page.locator('.app-main a[href="/audiobooks/abcdef12"]').first().click();expect(await page.locator('.reader-scroll').evaluate(el=>(window as unknown as {savedReader:Element}).savedReader===el)).toBeTruthy();
});
test('explicit library link persists on reopening without production',async({page})=>{
 const writes=await fixture(page);await page.goto('/library/book/abcdef12');await page.getByLabel('Passende Fassung').selectOption('prj-1');await page.getByRole('button',{name:'Verknüpfen',exact:true}).click();
 await expect(page.getByRole('button',{name:'Verknüpfung lösen'})).toBeVisible();await page.reload();await expect(page.getByRole('button',{name:'Verknüpfung lösen'})).toBeVisible();expect(writes).toEqual(['POST /api/library/links']);
});
test('quality opens exact cue and saving never starts rendering',async({page})=>{
 const writes=await fixture(page);await page.goto('/hoerspiele/prj-1');await page.getByRole('button',{name:/^Prüfen/}).click();await page.getByRole('combobox',{name:'Folge',exact:true}).selectOption('episode-1');await page.getByRole('button',{name:/Passage öffnen/}).click();
 await expect(page.locator('#cue-'+cue.id)).toBeFocused();await page.getByRole('button',{name:'Text bearbeiten'}).click();await page.getByLabel('Erzählpassage').fill('Eine korrigierte Passage.');await page.getByRole('button',{name:'Entwurf speichern'}).click();await expect(page.getByText('Eine korrigierte Passage.',{exact:true})).toBeVisible();expect(writes).toEqual(['PATCH /api/hoerspiele/projects/prj-1/cues/'+cue.id]);
});
test('file player keeps playing across route navigation',async({page})=>{
 await fixture(page);await page.goto('/podcast');await page.getByRole('button',{name:'Anhören',exact:true}).click();
 const player=page.getByRole('region',{name:'Gemeinsamer Player'});await expect(player.getByRole('button',{name:'Pause',exact:true})).toBeVisible();
 await page.getByRole('link',{name:/Sprecher \(/}).first().click();await expect(player.getByRole('button',{name:'Pause',exact:true})).toBeVisible(); await expect(player.getByText('Eine Episode',{exact:true})).toBeVisible();
});
test('cancel publishing sends no publication request',async({page})=>{
 const writes=await fixture(page);await page.goto('/audiobooks');await page.getByRole('button',{name:'Menü für Ein gemeinsames Werk'}).click();
 page.once('dialog',dialog=>dialog.dismiss());await page.getByRole('button',{name:'Im Store teilen'}).click();expect(writes.filter(w=>w.includes('/publish'))).toEqual([]);
});
test('chapter loading failure is retryable and not presented as empty',async({page})=>{
 await fixture(page);let fail=true;await page.route('**/api/audiobooks/abcdef12/content?*',route=>fail?route.fulfill({status:503,body:'Temporarily unavailable'}):route.fallback());
 await page.goto('/audiobooks/abcdef12');await expect(page.getByRole('button',{name:'Erneut laden'})).toBeVisible();await expect(page.getByText('Kein Text in diesem Kapitel.')).toHaveCount(0);fail=false;await page.getByRole('button',{name:'Erneut laden'}).click();await expect(page.getByText('Gespeicherte letzte Stelle')).toBeVisible();
});
test('book audio remains playing after leaving the reader',async({page})=>{
 await fixture(page);await page.goto('/audiobooks/abcdef12');await expect(page.getByText('Gespeicherte letzte Stelle')).toBeVisible();await page.getByRole('button',{name:/Abspielen|Wiedergabe starten/}).first().click();
 await page.locator('.reader-workspace a[href="/audiobooks"]').first().click();const player=page.getByRole('region',{name:'Gemeinsamer Player'});await expect(player.getByRole('button',{name:'Pause',exact:true})).toBeVisible();await page.getByRole('button',{name:'Pause',exact:true}).click();await expect(player.getByRole('button',{name:'Abspielen',exact:true})).toBeVisible();
});
test('visual acceptance on Fold window profiles and enlarged text',async({page},info)=>{
 test.skip(info.project.name !== 'chromium-desktop','Profiles run once');test.setTimeout(180000);await fixture(page);
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
 const profiles=[['cover-portrait',499,789],['cover-landscape',789,499],['inner-portrait',739,979],['inner-landscape',979,739],['desktop',1440,960]] as const;
 for(const [name,width,height] of profiles){
  await page.setViewportSize({width,height});
  await page.goto('/library');await expect(page.getByRole('heading',{name:'Deine Bibliothek'})).toBeVisible();
  await page.screenshot({animations:'disabled',path:`../docs/implementation/frontend-20-tage/verified-${name}-library.png`});
  expect(await page.locator('.app-main').evaluate(el=>el.scrollWidth <= el.clientWidth+1)).toBeTruthy();
  await page.goto('/hoerspiele/prj-1');await page.getByRole('button',{name:/^Prüfen/}).click();await expect(page.getByRole('button',{name:/Passage öffnen/})).toBeVisible();
  await page.screenshot({animations:'disabled',path:`../docs/implementation/frontend-20-tage/verified-${name}-quality.png`});
  expect(await page.locator('.app-main').evaluate(el=>el.scrollWidth <= el.clientWidth+1)).toBeTruthy();
  await page.goto('/podcast');await page.getByRole('button',{name:'Neuer Podcast',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Neuer Podcast'});await expect(dialog).toBeVisible();
  await dialog.evaluate(root=>{ const elements=[...root.querySelectorAll<HTMLElement>('*')].filter(el=>[...el.childNodes].some(n=>n.nodeType===Node.TEXT_NODE && n.textContent?.trim())); const sizes=elements.map(el=>parseFloat(getComputedStyle(el).fontSize));elements.forEach((el,i)=>el.style.fontSize=`${sizes[i]*1.25}px`); });
  expect(await dialog.evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBeTruthy();
  await page.screenshot({animations:'disabled',path:`../docs/implementation/frontend-20-tage/verified-${name}-dialog125.png`});
 }
 expect(errors).toEqual([]);
});
test('opening another book keeps the original audio until playback is switched',async({page})=>{
 await fixture(page);const other={...book,id:'1234abcd',title:'Ein anderes Buch'};
 await page.route('**/api/audiobooks',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({books:[book,other]})}));
 await page.route(/\/api\/audiobooks\/1234abcd(?:\?.*)?$/, r=>r.fulfill({contentType:'application/json',body:JSON.stringify(other)}));
 await page.route('**/api/audiobooks/1234abcd/content?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({segments:[{index:1,chapterIndex:1,text:'Anderer Buchtext',paragraphBreak:false}]})}));
 await page.goto('/audiobooks/abcdef12');await page.getByRole('button',{name:/Abspielen|Wiedergabe starten/}).first().click();await page.locator('.reader-workspace a[href="/audiobooks"]').first().click();
 await page.locator('.app-main-inner a[href="/audiobooks/1234abcd"]').first().click();await expect(page.getByText('Anderer Buchtext',{exact:true})).toBeVisible();
 const player=page.getByRole('region',{name:'Gemeinsamer Player'});await expect(player.getByRole('button',{name:'Pause',exact:true})).toBeVisible();await expect(player.getByText('Ein gemeinsames Werk',{exact:true})).toBeVisible();
});
test('podcast creation keeps a failed first source as an editable draft',async({page})=>{
 const writes=await fixture(page);const created={...podcast,id:'pod-2',topic:'Neue Episode',status:'draft',script:null,audio_path:null};
 await page.route('**/api/podcasts',r=>r.request().method()==='POST'?r.fulfill({contentType:'application/json',body:JSON.stringify(created)}):r.fallback());
 await page.route('**/api/podcasts/pod-2/sources/text',r=>r.fulfill({status:503,body:'Temporary failure'}));
 await page.route('**/api/podcasts/pod-2/sources',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({sources:[]})}));
 await page.goto('/podcast');await page.getByRole('button',{name:'Neuer Podcast',exact:true}).click();
 const dialog=page.getByRole('dialog',{name:'Neuer Podcast'});await dialog.getByLabel('Thema',{exact:true}).fill('Neue Episode');await dialog.getByLabel('Erste Textquelle (optional)').fill('Meine noch nicht gespeicherte Quelle.');await dialog.getByRole('combobox',{name:'Format',exact:true}).selectOption('monolog');await dialog.getByRole('checkbox',{name:/Mira/}).check();await dialog.getByRole('button',{name:'Anlegen',exact:true}).click();
 await expect(dialog).toBeHidden();await expect(page.getByText(/Die erste Quelle konnte nicht gespeichert werden/)).toBeVisible();await expect(page.locator('textarea').filter({visible:true})).toHaveValue('Meine noch nicht gespeicherte Quelle.');expect(writes.some(w=>w.includes('/generate'))).toBeFalsy();
});

for (const trigger of ['interval', 'focus'] as const) {
 test(`drama discovers an external completed repair after failure (${trigger})`, async ({page}, testInfo) => {
  const writes = await fixture(page);
  await page.clock.install();
  let repaired = false;
  let projectReads = 0;
  const failed = {id:'old-failed', project_id:project.id, status:'failed', stage:'writing', created_at:stamp, completed_units:8, total_units:8, error:'Szenenausrichtungs-Paket 5 wurde abgelehnt'};
  const completed = {...failed, id:'new-completed', status:'succeeded', stage:'completed', created_at:'2026-09-06T17:06:59Z', error:null};
  await page.route('**/api/hoerspiele/pipeline-runs?*', route => route.fulfill({json:repaired ? [completed,failed] : [failed]}));
  await page.route('**/api/hoerspiele/projects/prj-1', route => {
   projectReads++;
   return route.fulfill({json:repaired ? {...project,status:'completed'} : {...project,status:'writing',cues:[],artifacts:[]}});
  });
  await page.goto('/hoerspiele/prj-1');
  await expect(page.getByText(failed.error,{exact:true})).toBeVisible();
  if(trigger === 'interval') {
   await page.screenshot({path:`../docs/operations/dragonball-band-5-repair/stale-ui-before-${testInfo.project.name}.png`});
   await page.clock.fastForward(15000);
   expect(projectReads).toBe(1);
  }
  repaired = true;
  if(trigger === 'interval') await page.clock.fastForward(15000);
  else await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('heading',{name:'Aktuelle Audiofassung'})).toBeVisible();
  await expect(page.getByText(failed.error,{exact:true})).toHaveCount(0);
  expect(writes).toEqual([]);
  if(trigger === 'interval') await page.screenshot({path:`../docs/operations/dragonball-band-5-repair/stale-ui-after-${testInfo.project.name}.png`});
 });
}

test('drama discovers an external running repair and follows its completion', async ({page}) => {
 const writes = await fixture(page);
 await page.clock.install();
 let status = 'failed';
 const run = () => ({id:status === 'failed' ? 'old' : 'repair', project_id:project.id, status, stage:status === 'succeeded' ? 'completed' : 'writing', message:'Externer Reparaturlauf', created_at:stamp, completed_units:0,total_units:8,error:status === 'failed' ? 'Alter Abbruch' : null});
 await page.route('**/api/hoerspiele/pipeline-runs?*', route => route.fulfill({json:[run()]}));
 await page.route('**/api/hoerspiele/pipeline-runs/repair', route => route.fulfill({json:run()}));
 await page.route('**/api/hoerspiele/projects/prj-1', route => route.fulfill({json:{...project,status}}));
 await page.goto('/hoerspiele/prj-1');
 await expect(page.getByRole('alert')).toHaveText('Alter Abbruch');
 status = 'running';
 await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
 await expect(page.getByRole('button',{name:'Neu rendern'})).toBeDisabled();
 await expect(page.getByText(/Ein Lauf ist aktiv: Externer Reparaturlauf/)).toBeVisible();
 status = 'succeeded';
 await page.clock.fastForward(1000);
 await expect(page.getByRole('button',{name:'Neu rendern'})).toBeEnabled();
 await expect(page.getByText(/Ein Lauf ist aktiv:/)).toHaveCount(0);
 expect(writes).toEqual([]);
});

test('mode switches restore the podcast view and unsaved text', async({page})=>{
 const writes=await fixture(page);
 await page.goto('/podcast');
 await page.getByRole('button',{name:/^Skript/}).click();
 await page.getByRole('button',{name:'Bearbeiten',exact:true}).click();
 await page.getByLabel('Gesprochener Text').fill('Dieser Entwurf bleibt beim Moduswechsel.');
 const modes=page.locator('nav[aria-label="Hauptmodi"]:visible');
 await modes.getByRole('link',{name:/^Hörspiele/}).click();
 await expect(page).toHaveURL(/\/hoerspiele$/);
 await modes.getByRole('link',{name:/^Podcasts/}).click();
 await expect(page).toHaveURL(/view=script/);
 await expect(page.getByText('Ungespeicherter Entwurf · zum Fortsetzen bearbeiten')).toBeVisible();
 await page.getByRole('button',{name:'Bearbeiten',exact:true}).click();
 await expect(page.getByLabel('Gesprochener Text')).toHaveValue('Dieser Entwurf bleibt beim Moduswechsel.');
 expect(writes).toEqual([]);
});

test('shared library retains drama context and returns to the same quality view',async({page})=>{
 await fixture(page);
 await page.goto('/hoerspiele/prj-1');await page.getByRole('button',{name:/^Prüfen/}).click();
 await page.locator('a[href="/library/play/prj-1"]').click();
 const modes=page.locator('nav[aria-label="Hauptmodi"]:visible');
 await expect(modes.getByRole('link',{name:/^Hörspiele/})).toHaveAttribute('aria-current','page');
 await modes.getByRole('link',{name:/^Podcasts/}).click();
 await modes.getByRole('link',{name:/^Hörspiele/}).click();
 await expect(page).toHaveURL(/\/hoerspiele\/prj-1\?view=quality/);
 await expect(page.getByRole('button',{name:/Passage öffnen/})).toBeVisible();
});

test('reader mode menu keeps audio and the reader instance across modes',async({page})=>{
 await fixture(page);await page.goto('/audiobooks/abcdef12');
 await expect(page.getByText('Gespeicherte letzte Stelle')).toBeVisible();
 await page.locator('.reader-scroll').evaluate(el=>{(window as unknown as {savedReader:Element}).savedReader=el;});
 await page.getByRole('button',{name:'Abspielen',exact:true}).click();
 await page.getByRole('button',{name:'Modus wechseln'}).click();
 await page.getByRole('dialog',{name:'Modus wechseln'}).getByRole('link',{name:/^Hörspiele/}).click();
 await expect(page.getByRole('region',{name:'Gemeinsamer Player'}).getByRole('button',{name:'Pause',exact:true})).toBeVisible();
 await page.locator('nav[aria-label="Hauptmodi"]:visible').getByRole('link',{name:/^Hörbücher/}).click();
 await expect(page.getByText('Gespeicherte letzte Stelle')).toBeVisible();
 expect(await page.locator('.reader-scroll').evaluate(el=>(window as unknown as {savedReader:Element}).savedReader===el)).toBeTruthy();
});

test('native navigation closes a dialog before leaving the workflow',async({page})=>{
 const writes=await fixture(page);await page.goto('/podcast');
 await page.getByRole('button',{name:'Neuer Podcast',exact:true}).click();
 expect(await page.evaluate(()=>window.__vocariumBack?.())).toBe(true);
 await expect(page.getByRole('dialog')).toHaveCount(0);
 await page.evaluate(()=>window.__vocariumNavigate?.('/hoerspiele'));
 await expect(page).toHaveURL(/\/hoerspiele$/);
 expect(writes).toEqual([]);
});

test('speech keeps its draft and generated clip while modes share one player',async({page})=>{
 await fixture(page);
 await page.route('**/api/generate',r=>r.fulfill({contentType:'audio/wav',body:wav()}));
 await page.goto('/');
 await page.getByPlaceholder('Schreib oder füge ein, was die Stimme sagen soll…').fill('Ein Entwurf im Sprachstudio.');
 await page.getByRole('button',{name:'Generieren',exact:true}).click();
 const transport=page.getByRole('region',{name:'Gemeinsamer Player'});
 await expect(transport.getByText('Sprachaufnahme')).toBeVisible();
 const modes=page.locator('nav[aria-label="Hauptmodi"]:visible');
 await modes.getByRole('link',{name:/^Podcasts/}).click();
 await expect(transport.getByRole('button',{name:'Pause',exact:true})).toBeVisible();
 await page.getByRole('button',{name:/^Audio/}).click();
 await page.getByRole('button',{name:'Anhören',exact:true}).click();
 await expect(transport.getByText('Eine Episode',{exact:true})).toBeVisible();
 await modes.getByRole('link',{name:/^Sprachstudio/}).click();
 await expect(page.getByPlaceholder('Schreib oder füge ein, was die Stimme sagen soll…')).toHaveValue('Ein Entwurf im Sprachstudio.');
 await expect(page.getByTitle('WAV herunterladen')).toBeVisible();
 await expect(transport.getByText('Eine Episode',{exact:true})).toBeVisible();
});
