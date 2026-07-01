import { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';

const VoicesPage = lazy(() => import('./pages/VoicesPage'));
const SpeechPage = lazy(() => import('./pages/SpeechPage'));
const ClonePage = lazy(() => import('./pages/ClonePage'));
const CustomPage = lazy(() => import('./pages/CustomPage'));
const DesignPage = lazy(() => import('./pages/DesignPage'));
const BenchmarkPage = lazy(() => import('./pages/BenchmarkPage'));
const TranscribePage = lazy(() => import('./pages/TranscribePage'));
const MusicPage = lazy(() => import('./pages/MusicPage'));
const SoundEffectsPage = lazy(() => import('./pages/SoundEffectsPage'));
const PodcastPage = lazy(() => import('./pages/PodcastPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));

export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={null}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<SpeechPage />} />
            <Route path="/voices" element={<VoicesPage />} />
            <Route path="/clone" element={<ClonePage />} />
            <Route path="/custom" element={<CustomPage />} />
            <Route path="/design" element={<DesignPage />} />
            <Route path="/transcribe" element={<TranscribePage />} />
            <Route path="/music" element={<MusicPage />} />
            <Route path="/sfx" element={<SoundEffectsPage />} />
            <Route path="/podcast" element={<PodcastPage />} />
            <Route path="/benchmark" element={<BenchmarkPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
