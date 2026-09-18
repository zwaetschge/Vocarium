import {createContext, useContext, useEffect, useState, type ReactNode} from 'react';
import {useLocation} from 'react-router-dom';
import {areaOf, AREAS, type Area} from '../lib/navigation';

const initialRoutes = Object.fromEntries(AREAS.map(a => [a.id, a.home])) as Record<Area,string>;
const Context = createContext({area:'audiobooks' as Area, destinations:initialRoutes});

/** Only route positions live here; editor content stays in EditorDrafts. */
export function WorkspacesProvider({children}:{children:ReactNode}) {
 const location = useLocation();
 const [lastArea,setLastArea] = useState<Area>('audiobooks');
 const [destinations,setDestinations] = useState(initialRoutes);
 const area = areaOf(location.pathname,lastArea);
 useEffect(()=>{
  setLastArea(area);
  // Shared utilities are not a replacement for a mode's last working document.
  if(location.pathname.startsWith('/library') || location.pathname.startsWith('/settings')) return;
  const href=location.pathname+location.search+location.hash;
  setDestinations(previous => previous[area] === href ? previous : {...previous,[area]:href});
 },[area,location.pathname,location.search,location.hash]);
 return <Context.Provider value={{area,destinations}}>{children}</Context.Provider>;
}
export function useWorkspaces() {return useContext(Context);}
