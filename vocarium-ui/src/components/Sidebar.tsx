import { useLocation, Link } from 'react-router-dom';
import { motion } from 'framer-motion';
import { useState, useEffect } from 'react';
import { getHealth, getMe } from '../api';
import type { User } from '../types';
import StatusBadge from './StatusBadge';

type NavItem = { path: string; label: string; section: 'studio' | 'library' | 'tools'; icon: JSX.Element };

const navItems: NavItem[] = [
  {
    path: '/',
    label: 'Speech',
    section: 'studio',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10 1v7M10 12v7M6 4v12M14 4v12M2 7v6M18 7v6" />
      </svg>
    ),
  },
  {
    path: '/music',
    label: 'Music',
    section: 'studio',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="6" cy="16" r="2.5" />
        <circle cx="16" cy="13" r="2.5" />
        <path d="M8.5 16V5l10-3v11" />
      </svg>
    ),
  },
  {
    path: '/sfx',
    label: 'Sound Effects',
    section: 'studio',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M11 5L6 9H2v2h4l5 4V5z" />
        <path d="M15.54 6.46a5 5 0 010 7.07" />
        <path d="M18.07 3.93a9 9 0 010 12.14" />
      </svg>
    ),
  },
  {
    path: '/podcast',
    label: 'Podcast',
    section: 'studio',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="7" y="2" width="6" height="10" rx="3" />
        <path d="M4 9v1a6 6 0 0012 0V9" />
        <path d="M10 16v3M7 19h6" />
      </svg>
    ),
  },
  {
    path: '/voices',
    label: 'Voices',
    section: 'library',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="5" r="3" />
        <path d="M3 18v-1a5 5 0 015-5h4a5 5 0 015 5v1" />
      </svg>
    ),
  },
  {
    path: '/clone',
    label: 'Clone',
    section: 'library',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="7" r="4" />
        <path d="M3 18v-1a5 5 0 015-5h4a5 5 0 015 5v1" />
        <path d="M15 3l2 2-2 2" />
      </svg>
    ),
  },
  {
    path: '/custom',
    label: 'Custom',
    section: 'library',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="10" r="7.5" />
        <circle cx="10" cy="8" r="2.2" />
        <path d="M5.5 16.5a4.5 4.5 0 019 0" />
        <path d="M15.5 4.5l2 2" />
      </svg>
    ),
  },
  {
    path: '/design',
    label: 'Design',
    section: 'library',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 17l4-1L16.5 6.5a2.12 2.12 0 00-3-3L4 13l-1 4z" />
        <path d="M13.5 3.5l3 3" />
      </svg>
    ),
  },
  {
    path: '/transcribe',
    label: 'Transcribe',
    section: 'tools',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 5h12M4 9h8M4 13h10M4 17h6" />
      </svg>
    ),
  },
  {
    path: '/benchmark',
    label: 'Benchmark',
    section: 'tools',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 17V10M7 17V5M11 17V12M15 17V3M19 17V8" />
      </svg>
    ),
  },
  {
    path: '/settings',
    label: 'Settings',
    section: 'tools',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="10" r="3" />
        <path d="M10 1v4M10 15v4M3.5 3.5l2.8 2.8M13.7 13.7l2.8 2.8M1 10h4M15 10h4M3.5 16.5l2.8-2.8M13.7 6.3l2.8-2.8" />
      </svg>
    ),
  },
];

const sectionLabels: Record<NavItem['section'], string> = {
  studio: 'Studio',
  library: 'Voices',
  tools: 'Tools',
};

export default function Sidebar() {
  const location = useLocation();
  const [apiStatus, setApiStatus] = useState<'online' | 'offline' | 'loading'>('loading');
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    const check = () => {
      getHealth()
        .then(() => setApiStatus('online'))
        .catch(() => setApiStatus('offline'));
    };
    check();
    const id = setInterval(check, 10000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    getMe().then(setUser).catch(() => {});
  }, []);

  const sections: Array<NavItem['section']> = ['studio', 'library', 'tools'];

  return (
    <aside
      className="glass"
      style={{
        position: 'fixed',
        left: 0,
        top: 0,
        bottom: 0,
        width: '260px',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 30,
        borderTop: 'none',
        borderLeft: 'none',
        borderBottom: 'none',
        borderRadius: 0,
      }}
    >
      {/* Brand */}
      <div style={{ position: 'relative', padding: '26px 22px 22px', display: 'flex', alignItems: 'center', gap: '12px' }}>
        <BrandMark />
        <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
          <h1
            style={{
              fontSize: '19px',
              fontWeight: 600,
              fontFamily: 'var(--font-display)',
              letterSpacing: '-0.035em',
              color: 'var(--color-text)',
              lineHeight: 1,
            }}
          >
            Vocarium
          </h1>
          <p className="label-eyebrow" style={{ fontSize: '10px' }}>
            Voice Studio
          </p>
        </div>
      </div>

      <div style={{ margin: '0 22px', height: '1px', background: 'linear-gradient(to right, transparent, rgba(255,255,255,0.1), transparent)' }} />

      {/* Nav */}
      <nav style={{ flex: 1, overflowY: 'auto', padding: '18px 14px 10px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
        {sections.map((section) => {
          const items = navItems.filter((n) => n.section === section);
          return (
            <div key={section} style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
              <div className="label-eyebrow" style={{ padding: '0 10px 8px', fontSize: '10px' }}>
                {sectionLabels[section]}
              </div>
              {items.map((item) => {
                const active = location.pathname === item.path;
                return (
                  <Link key={item.path} to={item.path} style={{ textDecoration: 'none' }}>
                    <motion.div
                      whileTap={{ scale: 0.98 }}
                      style={{
                        position: 'relative',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '12px',
                        padding: '9px 12px',
                        borderRadius: '10px',
                        fontSize: '13.5px',
                        fontWeight: 500,
                        letterSpacing: '-0.005em',
                        color: active ? 'var(--color-text)' : 'var(--color-text-secondary)',
                        background: active ? 'rgba(123, 97, 255, 0.14)' : 'transparent',
                        border: active ? '1px solid rgba(123, 97, 255, 0.28)' : '1px solid transparent',
                        transition: 'color 0.15s ease, background 0.15s ease, border-color 0.15s ease',
                      }}
                      onMouseEnter={(e) => {
                        if (!active) {
                          e.currentTarget.style.color = 'var(--color-text)';
                          e.currentTarget.style.background = 'rgba(255,255,255,0.04)';
                        }
                      }}
                      onMouseLeave={(e) => {
                        if (!active) {
                          e.currentTarget.style.color = 'var(--color-text-secondary)';
                          e.currentTarget.style.background = 'transparent';
                        }
                      }}
                    >
                      {active && (
                        <motion.span
                          layoutId="sidebar-active-marker"
                          style={{
                            position: 'absolute',
                            left: '-14px',
                            top: '50%',
                            transform: 'translateY(-50%)',
                            width: '3px',
                            height: '18px',
                            borderRadius: '0 999px 999px 0',
                            background: 'var(--color-accent)',
                            boxShadow: '0 0 10px rgba(123,97,255,0.6)',
                          }}
                          transition={{ type: 'spring', stiffness: 400, damping: 32 }}
                        />
                      )}
                      <span
                        style={{
                          flexShrink: 0,
                          color: active ? 'var(--color-accent)' : 'var(--color-text-dim)',
                          transition: 'color 0.15s',
                        }}
                      >
                        {item.icon}
                      </span>
                      <span>{item.label}</span>
                    </motion.div>
                  </Link>
                );
              })}
            </div>
          );
        })}
      </nav>

      <div style={{ margin: '0 22px', height: '1px', background: 'linear-gradient(to right, transparent, rgba(255,255,255,0.08), transparent)' }} />

      {/* Footer: user + status */}
      <div style={{ padding: '16px 20px 20px', display: 'flex', flexDirection: 'column', gap: '14px' }}>
        {user && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
            <div
              style={{
                width: '32px',
                height: '32px',
                borderRadius: '50%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: 'linear-gradient(135deg, rgba(123,97,255,0.35), rgba(255,77,210,0.25))',
                border: '1px solid rgba(123,97,255,0.35)',
                fontSize: '13px',
                fontWeight: 600,
                color: '#fff',
                textTransform: 'uppercase',
                flexShrink: 0,
                boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.2)',
              }}
            >
              {user.display_name.charAt(0)}
            </div>
            <div style={{ minWidth: 0 }}>
              <div
                style={{
                  fontSize: '13px',
                  fontWeight: 500,
                  color: 'var(--color-text)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {user.display_name}
              </div>
              <div style={{ fontSize: '11px', color: 'var(--color-text-dim)', fontFamily: 'var(--font-mono)' }}>
                {user.username}
              </div>
            </div>
          </div>
        )}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span className="label-eyebrow" style={{ fontSize: '10px' }}>
            API
          </span>
          <StatusBadge status={apiStatus} />
        </div>
      </div>
    </aside>
  );
}

function BrandMark() {
  return (
    <div
      style={{
        width: '38px',
        height: '38px',
        borderRadius: '11px',
        position: 'relative',
        overflow: 'hidden',
        background: 'linear-gradient(135deg, rgba(123,97,255,0.35), rgba(255,77,210,0.28) 55%, rgba(50,181,255,0.25))',
        border: '1px solid rgba(255,255,255,0.18)',
        boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.22), 0 4px 14px rgba(107,75,255,0.3)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
      }}
    >
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="#fff" strokeWidth="1.7" strokeLinecap="round">
        <path d="M4 11c0-3 2-5 4-5M18 11c0-3-2-5-4-5" opacity="0.85" />
        <path d="M11 4v14" />
        <path d="M7 8v6M15 8v6" opacity="0.7" />
      </svg>
    </div>
  );
}
