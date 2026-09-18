import {useEffect} from 'react';
import {useLocation,useNavigate} from 'react-router-dom';

declare global {interface Window {
 __vocariumNavigate?: (path:string)=>void;
 __vocariumBack?: ()=>boolean;
}}
/** Android navigation reuses the React document, including active audio. */
export default function NativeNavigation() {
 const navigate=useNavigate();
 const {pathname}=useLocation();
 useEffect(()=>{
  window.__vocariumNavigate=path=>{if(path.startsWith('/') && !path.startsWith('//')) navigate(path);};
  window.__vocariumBack=()=>{
   const dialog=document.querySelector<HTMLDialogElement>('dialog[open]');
   if(dialog) {dialog.dispatchEvent(new Event('cancel',{cancelable:true}));return true;}
   if(/^\/audiobooks\/(?!pronunciation$|stats$|store$)[^/]+$/.test(pathname)) {navigate('/audiobooks');return true;}
   return false;
  };
  return ()=>{delete window.__vocariumNavigate;delete window.__vocariumBack;};
 },[navigate,pathname]);
 return null;
}
