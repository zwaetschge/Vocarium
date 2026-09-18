import {useEffect,useRef} from 'react';
import {pause,playFile,seek,usePlayer} from '../state/player';

/** Finished speech clips use the same transport as books, podcasts and dramas. */
export function useSpeechAudio() {
 const player=usePlayer();
 const urls=useRef(new Map<string,string>());
 const owns=player.track?.key.startsWith('speech:')??false;
 useEffect(()=>()=>{urls.current.forEach(url=>URL.revokeObjectURL(url));},[]);
 return {
  playing:owns&&player.playing, progress:owns&&player.duration>0?player.time/player.duration:0,
  duration:owns?player.duration:0,currentId:owns?player.track!.key.slice(7):null,
  play:(blob:Blob,id='clip')=>{
   let url=urls.current.get(id);
   if(!url) {url=URL.createObjectURL(blob);urls.current.set(id,url);}
   if(urls.current.size>12) {const oldest=urls.current.keys().next().value;if(oldest && oldest!==id){URL.revokeObjectURL(urls.current.get(oldest)!);urls.current.delete(oldest);}}
   playFile({key:`speech:${id}`,title:'Sprachaufnahme',subtitle:'Sprachstudio',href:'/',url});
  },
  stop:()=>{if(owns)pause();},
  toggle:()=>{if(owns&&player.playing)pause();else if(owns&&player.track)playFile(player.track);},
  seek:(pct:number)=>{if(owns)seek(pct*player.duration);},
 };
}
