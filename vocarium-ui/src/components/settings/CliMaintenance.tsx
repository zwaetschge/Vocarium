import {useEffect, useRef, useState} from 'react';
import {hs} from '../../lib/hoerspiele';

type Maintenance = {
 job: null | {id:string; kind:string; provider?:string; status:string; message:string};
 clis: Record<string,{installed?:string;latest?:string;error?:string;checked_at?:string}>;
 catalogs: Record<string,{models?:{value:string;label:string}[];error?:string;refreshed_at?:string;checked_at?:string;source?:string}>;
};
const names:Record<string,string>={codex:'Codex CLI',claude:'Claude Code CLI',zai:'Z.AI'};
const endpoint='/settings/agents/maintenance';

export default function CliMaintenance() {
 const [data,setData]=useState<Maintenance|null>(null);
 const [error,setError]=useState('');
 const [pollError,setPollError]=useState('');
 const [pending,setPending]=useState(false);
 const completed=useRef('');
 const generation=useRef(0);
 const busy=pending||data?.job?.status==='running';
 useEffect(()=>{
  let alive=true;let timer:number;
  async function poll(){
   const requestGeneration=generation.current;
   try {
    const next=await hs<Maintenance>(endpoint);
    if(!alive||requestGeneration!==generation.current)return;
    setData(next);setPollError('');
    if(next.job?.status==='completed'&&next.job.id!==completed.current){
     completed.current=next.job.id;window.dispatchEvent(new Event('vocarium-models-updated'));
    }
   }catch(e){if(alive)setPollError((e as Error).message);}
   finally{if(alive)timer=window.setTimeout(poll,2500);}
  }
  void poll();return()=>{alive=false;window.clearTimeout(timer);};
 },[]);
 async function start(action:string){
  setPending(true);setError('');generation.current++;
  try{setData(await hs<Maintenance>(endpoint+'/'+action,{method:'POST'}));}
  catch(e){setError((e as Error).message);}
  finally{setPending(false);}
 }
 return <section className="card hs-panel hs-panel-wide">
  <div className="hs-panel-head"><h2>CLIs &amp; Modelllisten</h2></div>
  <p className="hs-item-note">Codex und Claude für Hörspiel-Recherche und Skripte verwalten. Updates starten nur im Leerlauf.</p>
  <div className="hs-actions">
   <button className="btn btn-outline" disabled={busy} onClick={()=>void start('check')}>Versionen prüfen</button>
   <button className="btn btn-primary" disabled={busy} onClick={()=>void start('models/refresh')}>Modelllisten aktualisieren</button>
  </div>
  {(error||pollError)&&<p role="alert" className="hs-error">{error||pollError}</p>}
  {!data&&!error&&!pollError&&<p role="status">Runner wird abgefragt …</p>}
  {data?.job&&<p role="status" className="maintenance-status" aria-live="polite">
   {busy?(data.job.kind==='update'?`${names[data.job.provider||'']||'CLI'} wird installiert und geprüft …`:data.job.kind==='models'?'Modelllisten werden abgerufen …':'CLI-Versionen werden geprüft …'):data.job.status==='failed'?'Update fehlgeschlagen. Die bisherige CLI bleibt aktiv. Bitte Verbindung und freien Speicher prüfen.':'Prüfung abgeschlossen. Ergebnisse stehen bei den Anbietern.'}
  </p>}
  <ul className="cli-maintenance-list">
   {['codex','claude'].map(provider=>{const cli=data?.clis[provider];return <li key={provider}>
    <h3>{names[provider]}</h3><p>Installiert: <strong>{cli?.installed||'Noch nicht geprüft'}</strong><br/>Neueste Version: <strong>{cli?.latest||'Noch nicht geprüft'}</strong></p>
    {cli?.error&&<p role="status">Versionsprüfung nicht erreichbar. Letzte bekannte Angaben bleiben sichtbar.</p>}
    {cli?.checked_at&&<p>Geprüft: {new Date(cli.checked_at).toLocaleString('de-DE')}</p>}
    <button className="btn btn-outline" disabled={busy||!data} onClick={()=>void start(`clis/${provider}/update`)}>{names[provider]} aktualisieren</button>
   </li>})}
  </ul>
  <h3>Modellkataloge</h3>
  <ul className="cli-maintenance-list">
   {['codex','claude','zai'].map(provider=>{const catalog=data?.catalogs[provider];return <li key={provider}>
    <strong>{names[provider]}</strong>
    <p>{catalog?.models?.length?`${catalog.models.length} Modelle · ${catalog.source}`:'Mitgelieferte Modellliste'}<br/>{catalog?.refreshed_at?`Aktualisiert: ${new Date(catalog.refreshed_at).toLocaleString('de-DE')}`:'Noch kein Katalog abgerufen'}</p>
    {catalog?.error&&<p role="status">Katalog nicht erreichbar. Letzte bekannte Modelle bleiben verfügbar; Zugang prüfen und erneut aktualisieren.</p>}
   </li>})}
  </ul>
  <p className="hs-item-note">Nach einem CLI-Update werden die Modelllisten automatisch aktualisiert. Deine Modellauswahl bleibt erhalten. Z.AI benötigt keine eigene CLI.</p>
 </section>;
}
