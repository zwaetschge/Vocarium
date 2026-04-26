import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import VoicesPage from './pages/VoicesPage';
import SpeechPage from './pages/SpeechPage';
import ClonePage from './pages/ClonePage';
import CustomPage from './pages/CustomPage';
import DesignPage from './pages/DesignPage';
import BenchmarkPage from './pages/BenchmarkPage';
import TranscribePage from './pages/TranscribePage';
import MusicPage from './pages/MusicPage';
import SoundEffectsPage from './pages/SoundEffectsPage';
import PodcastPage from './pages/PodcastPage';
import SettingsPage from './pages/SettingsPage';

export default function App() {
  return (
    <BrowserRouter>
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
    </BrowserRouter>
  );
}
