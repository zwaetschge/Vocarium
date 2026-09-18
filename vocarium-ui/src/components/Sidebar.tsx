import {Link,useLocation} from 'react-router-dom';
import {useEffect,useState} from 'react';
import {getMe} from '../api';
import type {User} from '../types';
import {AREAS,navItems} from '../lib/navigation';
import {useWorkspaces} from '../state/Workspaces';
import {ModeLinks} from './ModeNavigation';

export default function Sidebar() {
 const {pathname}=useLocation();
 const {area}=useWorkspaces();
 const [user,setUser]=useState<User|null>(null);
 useEffect(()=>{getMe().then(setUser).catch(()=>{});},[]);
 const home=AREAS.find(a=>a.id===area)!.home;
 const items=navItems.filter(item=>(item.area??'lab')===area).sort((a,b)=>Number(b.path===home)-Number(a.path===home));
 return <aside className="glass app-sidebar workflow-sidebar" aria-label="Navigation">
  <Link to={AREAS.find(a=>a.id===area)!.home} className="workspace-brand"><span className="workspace-brand-mark" aria-hidden="true">V</span><span>Vocarium<small>Dein Audiostudio</small></span></Link>
  <nav className="mode-list" aria-label="Hauptmodi"><ModeLinks/></nav>
  <div className="workspace-tools-label">{AREAS.find(a=>a.id===area)!.label}</div>
  <nav className="workspace-sidebar-tools" aria-label="Werkzeuge">
   {items.map(item=><Link key={item.path} to={item.path} aria-current={pathname===item.path?'page':undefined}>{item.icon}<span>{item.label}</span></Link>)}
  </nav>
  <footer className="workspace-account"><span className="workspace-avatar" aria-hidden="true">{user?.display_name?.charAt(0)||'V'}</span><span>{user?.display_name||'Vocarium'}<small>Persönlicher Arbeitsbereich</small></span><Link to="/settings/allgemein" aria-label="App-Einstellungen">⚙</Link></footer>
 </aside>;
}
