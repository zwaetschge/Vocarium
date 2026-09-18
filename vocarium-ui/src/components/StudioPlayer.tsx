import { Link, useLocation } from 'react-router-dom';
import { pause, playFile, resume, seek, setSpeed, setVolume, usePlayer, type PlayerTrack } from '../state/player';
const clock = (s:number) => `${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
export function PlayEdition({ track }: { track: PlayerTrack }) {
 const player=usePlayer(); const active=player.track?.key===track.key && player.playing;
 return <button type="button" className="btn btn-secondary" onClick={()=>active?pause():playFile(track)}>{active?'Pause':'Anhören'}</button>;
}
export default function StudioPlayer() {
 const player=usePlayer(); const location=useLocation();
 if (!player.track || (player.track.key.startsWith('book:') && location.pathname===player.track.href)) return null;
 return <section aria-label="Gemeinsamer Player" className="studio-player glass-strong">
  <div className="studio-player-title"><Link to={player.track.href}>{player.track.title}</Link><span>{player.track.subtitle}</span></div>
  <div className="studio-player-transport"><button className="btn btn-ghost" aria-label="15 Sekunden zurück" onClick={()=>seek(player.time-15)}>−15</button><button className="btn btn-primary" onClick={()=>player.playing?pause():void resume()}>{player.playing?'Pause':'Abspielen'}</button><button className="btn btn-ghost" aria-label="15 Sekunden vor" onClick={()=>seek(player.time+15)}>+15</button></div>
  <label className="studio-player-seek"><span>{clock(player.time)} / {clock(player.duration)}</span><input aria-label="Wiedergabeposition" type="range" min="0" max={player.duration || 1} step="0.1" value={player.time} onChange={e=>seek(Number(e.target.value))} /></label>
  <label>Tempo<select aria-label="Wiedergabetempo" value={player.speed} onChange={e=>setSpeed(Number(e.target.value))}>{[.75,1,1.25,1.5,1.75,2].map(s=><option key={s} value={s}>{s}×</option>)}</select></label>
  <label className="studio-player-volume">Lautstärke<input aria-label="Lautstärke" type="range" min="0" max="1" step="0.05" value={player.volume} onChange={e=>setVolume(Number(e.target.value))} /></label>
  {player.error && <p role="alert">{player.error}</p>}
 </section>;
}
