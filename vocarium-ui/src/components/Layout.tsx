import WorkspaceBoundary from './WorkspaceBoundary';
import NativeNavigation from './NativeNavigation';
import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { getSettingsPrefs } from '../api';
import type { GeneralPrefs } from '../types';
import { AREAS } from '../lib/navigation';
import WorkflowBar from './WorkflowBar';
import WorkflowGuide from './WorkflowGuide';
import { motion } from 'framer-motion';
import Aurora from './Aurora';
import Sidebar from './Sidebar';
import InstallPrompt from './InstallPrompt';
import BottomNav from './BottomNav';
import StudioPlayer from './StudioPlayer';
import { usePlayer } from '../state/player';
const SpeechPage = lazy(() => import('../pages/SpeechPage'));

function SpeechWorkspace({visible}:{visible:boolean}) {
  const [visited,setVisited]=useState(visible);
  useEffect(()=>{if(visible)setVisited(true);},[visible]);
  if(!visited && !visible)return null;
  return <div hidden={!visible} className="speech-workspace app-main-inner"><WorkflowGuide/><WorkspaceBoundary><Suspense fallback={<p role="status">Sprachstudio wird geladen…</p>}><SpeechPage visible={visible}/></Suspense></WorkspaceBoundary></div>;
}

const AudiobookReaderPage = lazy(() => import('../pages/AudiobookReaderPage'));

function ReaderWorkspace({ activeId }: { activeId: string }) {
  const [lastId, setLastId] = useState(activeId);
  useEffect(() => { if (activeId) setLastId(activeId); }, [activeId]);
  const player = usePlayer();
  const playingId = player.track?.key.startsWith('book:') ? player.track.href.split('/')[2] : '';
  // Keep at most the viewed book and the current playback book, even while
  // another book is opened. A new playback adopts and releases the old owner.
  const ids = [...new Set([activeId || lastId, playingId].filter(Boolean))];
  return <>{ids.map(id=><div key={id} hidden={id !== activeId} className="reader-workspace"><Suspense fallback={<p role="status">Reader wird geladen…</p>}><AudiobookReaderPage id={id} visible={id === activeId} /></Suspense></div>)}</>;

}

export default function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const startupRedirect = useRef(false);
  // Der Reader bringt seine eigene Glas-Topbar mit und blendet die
  // Bereichsnavigation aus.
  const isReader = /^\/audiobooks\/(?!pronunciation$|stats$|store$)[^/]+$/.test(location.pathname);

  // Startbereich (Einstellungen ▸ Allgemein): greift nur beim ersten Aufruf
  // von "/", damit ein späterer Klick auf "Lab" nicht wieder wegspringt.
  useEffect(() => {
    if (startupRedirect.current) return;
    startupRedirect.current = true;
    if (location.pathname !== '/') return;
    // Android already chose a remembered workspace or an explicit app link.
    if (/VocariumApp\//.test(navigator.userAgent)) return;
    getSettingsPrefs<GeneralPrefs>('general')
      .then((prefs) => {
        const home = AREAS.find((a) => a.id === prefs.area_default)?.home;
        if (home && home !== '/') navigate(home, { replace: true });
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <Aurora />
      <div className={`app-shell${isReader ? ' app-shell--reader' : ''}`}>
        <Sidebar />
        <div className="app-content">
          {!isReader && <WorkflowBar />}

          {/* Main scroll area */}
          <main className={`app-main${isReader ? ' app-main--reader' : ''}`}>
            <SpeechWorkspace visible={location.pathname==='/'} />
            <ReaderWorkspace activeId={isReader ? location.pathname.split('/')[2] : ''} />
            <motion.div
              hidden={isReader || location.pathname==='/'}
              key={location.pathname}
              initial={{ opacity: 0, y: 14 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.26, ease: [0.25, 0.46, 0.45, 0.94] }}
              className="app-main-inner"
            >
              <WorkflowGuide />
              <WorkspaceBoundary key={location.pathname}><Suspense fallback={<p role="status" className="page-loading">Arbeitsbereich wird geladen…</p>}><Outlet /></Suspense></WorkspaceBoundary>
            </motion.div>
          </main>
        </div>
      </div>
      <NativeNavigation />
      <StudioPlayer />
      <InstallPrompt />
      <BottomNav />
    </>
  );
}
