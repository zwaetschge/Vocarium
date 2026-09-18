import { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { MotionConfig } from 'framer-motion';
import Layout from './components/Layout';
import {WorkspacesProvider} from './state/Workspaces';
import { EditorDraftsProvider } from './state/EditorDrafts';

const LibraryPage = lazy(() => import('./pages/LibraryPage'));
const VoicesPage = lazy(() => import('./pages/VoicesPage'));
const ClonePage = lazy(() => import('./pages/ClonePage'));
const TranscribePage = lazy(() => import('./pages/TranscribePage'));
const PodcastPage = lazy(() => import('./pages/PodcastPage'));
const PodcastHostsPage = lazy(() => import('./pages/PodcastHostsPage'));
const AudiobooksPage = lazy(() => import('./pages/AudiobooksPage'));
const PronunciationPage = lazy(() => import('./pages/PronunciationPage'));
const AudiobookStatsPage = lazy(() => import('./pages/AudiobookStatsPage'));
const AudiobookStorePage = lazy(() => import('./pages/AudiobookStorePage'));
const HoerspieleProjectsPage = lazy(() => import('./pages/hoerspiele/HoerspieleProjectsPage'));
const HoerspielDetailPage = lazy(() => import('./pages/hoerspiele/HoerspielDetailPage'));
const HoerspieleActivityPage = lazy(() => import('./pages/hoerspiele/HoerspieleActivityPage'));
const HoerspieleSettingsPage = lazy(() => import('./pages/hoerspiele/HoerspieleSettingsPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));

export default function App() {
  return (
    <MotionConfig reducedMotion="user"><BrowserRouter>
      <WorkspacesProvider><EditorDraftsProvider>
      <Suspense fallback={<div role="status" className="page-loading">Ansicht wird geladen…</div>}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={null} />
            <Route path="/voices" element={<VoicesPage />} />
            <Route path="/clone" element={<ClonePage />} />
            <Route path="/transcribe" element={<TranscribePage />} />
            <Route path="/podcast" element={<PodcastPage />} />
            <Route path="/podcast/hosts" element={<PodcastHostsPage />} />
            <Route path="/library" element={<LibraryPage />} />
            <Route path="/library/:kind/:id" element={<LibraryPage />} />
            <Route path="/audiobooks" element={<AudiobooksPage />} />
            <Route path="/audiobooks/pronunciation" element={<PronunciationPage />} />
            <Route path="/audiobooks/stats" element={<AudiobookStatsPage />} />
            <Route path="/audiobooks/store" element={<AudiobookStorePage />} />
            <Route path="/audiobooks/:id" element={null} />
            <Route path="/hoerspiele" element={<HoerspieleProjectsPage />} />
            <Route path="/hoerspiele/aktivitaet" element={<HoerspieleActivityPage />} />
            <Route path="/hoerspiele/einstellungen" element={<HoerspieleSettingsPage />} />
            <Route path="/hoerspiele/:id" element={<HoerspielDetailPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/settings/:tab" element={<SettingsPage />} />
          </Route>
        </Routes>
      </Suspense>
    </EditorDraftsProvider></WorkspacesProvider>
    </BrowserRouter></MotionConfig>
  );
}
