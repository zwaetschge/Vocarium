import {useSearchParams} from 'react-router-dom';

/** Preserve a workflow's selected document/tab in its navigable address. */
export function useRouteField(key:string, fallback:string):[string,(value:string|null)=>void] {
 const [params,setParams]=useSearchParams();
 return [params.get(key) || fallback, value=>setParams(previous=>{
  const next=new URLSearchParams(previous);
  if(value) next.set(key,value); else next.delete(key);
  return next;
 },{replace:true})];
}
