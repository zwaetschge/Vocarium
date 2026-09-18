import {useEffect,useRef,useState} from 'react';
import {Link,useLocation} from 'react-router-dom';
import {AREAS,navItems} from '../lib/navigation';
import {useWorkspaces} from '../state/Workspaces';
import {ModeIcon,modeDescriptions} from './ModeNavigation';
import Dialog from './Dialog';

export default function WorkflowBar() {
 const {area}=useWorkspaces();
 const {pathname}=useLocation();
 const [open,setOpen]=useState(false);
 const tabs=useRef<HTMLElement>(null);
 const mode=AREAS.find(a=>a.id===area)!;
 const items=navItems.filter(n=>(n.area??'lab')===area).sort((a,b)=>Number(b.path===mode.home)-Number(a.path===mode.home));
 useEffect(()=>{
  setOpen(false);
  tabs.current?.querySelector('[aria-current="page"]')?.scrollIntoView({block:'nearest',inline:'nearest'});
 },[pathname,area]);
 return <header className="workflow-bar">
  <div className="workflow-identity"><ModeIcon area={area}/><Link to={mode.home}>{mode.label}</Link><span>{modeDescriptions[area]}</span><Link className="workflow-home" to={mode.home}>Übersicht</Link><Link className="workflow-settings" to="/settings/allgemein" aria-label="Einstellungen öffnen"><span aria-hidden="true">⚙</span><span>Einstellungen</span></Link><button className="btn btn-ghost btn-sm workflow-tools-button" onClick={()=>setOpen(true)} aria-label="Alle Werkzeuge öffnen">Werkzeuge</button></div>
  <nav ref={tabs} className="workflow-tabs" aria-label={`${mode.label}: Werkzeuge`}>
   {items.map(item=><Link key={item.path} to={item.path} aria-current={pathname===item.path?'page':undefined}>{item.label}</Link>)}
  </nav>
  {open && <Dialog title={`${mode.label}: Werkzeuge`} onClose={()=>setOpen(false)}><nav className="workspace-sidebar-tools" aria-label="Alle Werkzeuge">{items.map(item=><Link key={item.path} to={item.path} onClick={()=>setOpen(false)} aria-current={pathname===item.path?'page':undefined}>{item.icon}{item.label}</Link>)}</nav></Dialog>}
 </header>;
}
