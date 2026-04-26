import { Outlet, useLocation } from 'react-router-dom';
import { motion } from 'framer-motion';
import Aurora from './Aurora';
import Sidebar from './Sidebar';
import ModelSelector from './ModelSelector';

type PageMeta = { title: string; eyebrow: string; icon: JSX.Element };

const pageConfig: Record<string, PageMeta> = {
  '/': {
    eyebrow: 'Studio',
    title: 'Text to Speech',
    icon: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <path d="M11 1v8M11 13v8M7 4v14M15 4v14M3 7.5v7M19 7.5v7" />
      </svg>
    ),
  },
  '/voices': {
    eyebrow: 'Library',
    title: 'Voice Library',
    icon: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <circle cx="11" cy="6" r="4" />
        <path d="M3 20v-1a6 6 0 016-6h4a6 6 0 016 6v1" />
      </svg>
    ),
  },
  '/clone': {
    eyebrow: 'Capture',
    title: 'Voice Cloning',
    icon: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <circle cx="11" cy="8" r="4.5" />
        <path d="M3 20v-1a6 6 0 016-6h4a6 6 0 016 6v1" />
      </svg>
    ),
  },
  '/design': {
    eyebrow: 'Compose',
    title: 'Voice Designer',
    icon: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <path d="M3 19l5-1L18.5 7.5a2.5 2.5 0 00-3.5-3.5L5 14l-2 5z" />
      </svg>
    ),
  },
  '/transcribe': {
    eyebrow: 'Listen',
    title: 'Transcription',
    icon: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <path d="M4 6h14M4 11h10M4 16h12" />
      </svg>
    ),
  },
  '/music': {
    eyebrow: 'Produce',
    title: 'Music Studio',
    icon: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <circle cx="7" cy="17" r="3" />
        <circle cx="17" cy="14" r="3" />
        <path d="M10 17V6l10-3v11" />
      </svg>
    ),
  },
  '/sfx': {
    eyebrow: 'Render',
    title: 'Sound Effects',
    icon: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <path d="M12 5L7 9H3v4h4l5 4V5z" />
        <path d="M16.54 7.46a5.5 5.5 0 010 7.07" />
        <path d="M19.07 4.93a9.5 9.5 0 010 12.14" />
      </svg>
    ),
  },
  '/benchmark': {
    eyebrow: 'Measure',
    title: 'Benchmark',
    icon: (
      <svg width="18" height="18" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <path d="M3 19V10M7 19V5M11 19V12M15 19V3M19 19V8" />
      </svg>
    ),
  },
};

export default function Layout() {
  const location = useLocation();
  const page = pageConfig[location.pathname] ?? { eyebrow: 'Vocarium', title: 'Studio', icon: <></> };

  return (
    <>
      <Aurora />
      <div
        style={{
          position: 'relative',
          zIndex: 3,
          display: 'flex',
          height: '100vh',
          overflow: 'hidden',
        }}
      >
        <Sidebar />

        <div
          style={{
            flex: 1,
            marginLeft: '260px',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            minWidth: 0,
          }}
        >
          {/* Header — glass over aurora */}
          <header
            className="glass"
            style={{
              flexShrink: 0,
              height: '68px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '0 36px',
              borderLeft: 'none',
              borderRight: 'none',
              borderTop: 'none',
              borderRadius: 0,
              zIndex: 20,
            }}
          >
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.28, ease: [0.25, 0.46, 0.45, 0.94] }}
              style={{ display: 'flex', alignItems: 'center', gap: '14px' }}
            >
              <div
                style={{
                  width: '36px',
                  height: '36px',
                  borderRadius: '10px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: 'rgba(123, 97, 255, 0.12)',
                  border: '1px solid rgba(123, 97, 255, 0.22)',
                  color: 'var(--color-accent)',
                  boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.08)',
                }}
              >
                {page.icon}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                <span className="label-eyebrow" style={{ fontSize: '10px' }}>
                  {page.eyebrow}
                </span>
                <h2
                  style={{
                    fontSize: '18px',
                    fontWeight: 600,
                    fontFamily: 'var(--font-display)',
                    color: 'var(--color-text)',
                    letterSpacing: '-0.025em',
                    lineHeight: 1,
                  }}
                >
                  {page.title}
                </h2>
              </div>
            </motion.div>

            <ModelSelector />
          </header>

          {/* Main scroll area */}
          <main style={{ flex: 1, overflowY: 'auto', position: 'relative' }}>
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 14 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.26, ease: [0.25, 0.46, 0.45, 0.94] }}
              style={{
                padding: '40px 36px 72px',
                maxWidth: '1400px',
                margin: '0 auto',
              }}
            >
              <Outlet />
            </motion.div>
          </main>
        </div>
      </div>
    </>
  );
}
