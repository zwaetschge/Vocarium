import {useEffect,useRef} from 'react';
import {useLocation} from 'react-router-dom';
import {ModeLinks} from './ModeNavigation';

/** Four stable destinations; measured height also supports Android text zoom. */
export default function BottomNav() {
 const {pathname}=useLocation();
 const ref=useRef<HTMLElement>(null);
 useEffect(()=>{
  const update=()=>document.documentElement.style.setProperty('--mode-nav-height',`${window.innerWidth<720 ? ref.current?.getBoundingClientRect().height??0 : 0}px`);
  const observer=new ResizeObserver(update);
  if(ref.current)observer.observe(ref.current);
  update();window.addEventListener('resize',update);
  return ()=>{observer.disconnect();window.removeEventListener('resize',update);document.documentElement.style.setProperty('--mode-nav-height','0px');};
 },[pathname]);
 if (/^\/audiobooks\/(?!pronunciation$|stats$|store$)[^/]+$/.test(pathname)) return null;
 return <nav ref={ref} className="bottom-nav glass mode-bottom" aria-label="Hauptmodi"><ModeLinks compact/></nav>;
}
