import {useState} from 'react';
import {Link} from 'react-router-dom';
import {AREAS, type Area} from '../lib/navigation';
import {useWorkspaces} from '../state/Workspaces';
import Dialog from './Dialog';

export const modeDescriptions:Record<Area,string>={
 audiobooks:'Bücher lesen und vertonen', podcasts:'Episoden schreiben und produzieren',
 hoerspiele:'Roman und Originalton verbinden', lab:'Text, Stimmen und Transkription',
};
export function ModeIcon({area}:{area:Area}) {
 const paths:Record<Area,JSX.Element>={
  audiobooks:<><path d="M3 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-2H3zM21 4h-6a3 3 0 0 0-3 3v14a4 4 0 0 1 4-2h5z"/></>,
  podcasts:<><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/></>,
  hoerspiele:<><path d="M3 13v-2a9 9 0 0 1 18 0v2M7 12H3v8h4zM17 12h4v8h-4zM10 9l5 3-5 3z"/></>,
  lab:<path d="M3 9v6M7 5v14M12 2v20M17 5v14M21 9v6"/>,
 };
 return <svg aria-hidden="true" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{paths[area]}</svg>;
}
export function ModeLinks({onNavigate,compact=false}:{onNavigate?:()=>void;compact?:boolean}) {
 const {area,destinations}=useWorkspaces();
 return <>{AREAS.map(mode=><Link key={mode.id} className={`mode-link${area===mode.id?' is-active':''}`} to={destinations[mode.id]} aria-current={area===mode.id?'page':undefined} onClick={onNavigate}>
  <ModeIcon area={mode.id}/><span><strong>{mode.label}</strong>{!compact && <small>{modeDescriptions[mode.id]}</small>}</span>
 </Link>)}</>;
}
export function ModeMenuButton() {
 const [open,setOpen]=useState(false);
 return <><button className="btn btn-ghost btn-sm" aria-label="Modus wechseln" onClick={()=>setOpen(true)}>Modi</button>{open && <Dialog title="Modus wechseln" onClose={()=>setOpen(false)}><nav className="mode-list" aria-label="Hauptmodi"><ModeLinks onNavigate={()=>setOpen(false)}/></nav></Dialog>}</>;
}
