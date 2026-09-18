export type Area = 'audiobooks' | 'podcasts' | 'hoerspiele' | 'lab';
export type NavItem = { path: string; label: string; section: 'studio' | 'library' | 'tools'; icon: JSX.Element; area?: Area };

export const AREAS: { id: Area; label: string; home: string }[] = [
  { id: 'audiobooks', label: 'Hörbücher', home: '/audiobooks' },
  { id: 'podcasts', label: 'Podcasts', home: '/podcast' },
  { id: 'hoerspiele', label: 'Hörspiele', home: '/hoerspiele' },
  { id: 'lab', label: 'Sprachstudio', home: '/' },
];

export function areaOf(pathname: string, fallback: Area = 'audiobooks'): Area {
  if (pathname.startsWith('/library/play/')) return 'hoerspiele';
  if (pathname.startsWith('/library/book/')) return 'audiobooks';
  if (pathname.startsWith('/library')) return fallback;
  if (pathname.startsWith('/audiobooks') || pathname.startsWith('/settings/hoerbuecher')) return 'audiobooks';
  if (pathname.startsWith('/podcast') || pathname.startsWith('/settings/podcasts')) return 'podcasts';
  if (pathname.startsWith('/hoerspiele') || pathname.startsWith('/settings/hoerspiele')) return 'hoerspiele';
  if (pathname.startsWith('/settings/lab')) return 'lab';
  if (pathname.startsWith('/settings')) return fallback;
  return 'lab';
}

const settingsIcon = (
  <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="10" cy="10" r="3" />
    <path d="M10 1v4M10 15v4M3.5 3.5l2.8 2.8M13.7 13.7l2.8 2.8M1 10h4M15 10h4M3.5 16.5l2.8-2.8M13.7 6.3l2.8-2.8" />
  </svg>
);

/** Jeder Bereich öffnet den Reiter, der zu ihm gehört. */
const SETTINGS_PATH: Record<Area, string> = {
  audiobooks: '/settings/hoerbuecher',
  podcasts: '/settings/podcasts',
  hoerspiele: '/settings/hoerspiele',
  lab: '/settings/allgemein',
};

export const navItems: NavItem[] = [
  { path: '/library', label: 'Gemeinsames Regal', section: 'studio', area: 'audiobooks', icon: <span aria-hidden="true">▤</span> },
  { path: '/library', label: 'Gemeinsames Regal', section: 'library', area: 'hoerspiele', icon: <span aria-hidden="true">▤</span> },
  {
    path: '/audiobooks',
    label: 'Hörbücher',
    section: 'studio',
    area: 'audiobooks',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 4a2 2 0 012-2h5v16H5a2 2 0 00-2 2V4z" />
        <path d="M10 2h5a2 2 0 012 2v16a2 2 0 00-2-2h-5" />
      </svg>
    ),
  },
  {
    path: '/audiobooks/store',
    label: 'Bücher entdecken',
    section: 'studio',
    area: 'audiobooks',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 7l1.5-4h11L17 7M3 7h14M3 7v10a1 1 0 001 1h12a1 1 0 001-1V7" />
        <path d="M8 11h4" />
      </svg>
    ),
  },
  {
    path: '/audiobooks/stats',
    label: 'Statistiken',
    section: 'studio',
    area: 'audiobooks',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 17V9M8 17V4M13 17v-6M18 17V7" />
      </svg>
    ),
  },
  {
    path: '/audiobooks/pronunciation',
    label: 'Aussprache',
    section: 'studio',
    area: 'audiobooks',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 15V5a1 1 0 011-1h6l5 4-5 4H5" />
        <path d="M7 19h6" />
      </svg>
    ),
  },
  {
    path: '/',
    label: 'Text vertonen',
    section: 'studio',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10 1v7M10 12v7M6 4v12M14 4v12M2 7v6M18 7v6" />
      </svg>
    ),
  },
  {
    path: '/podcast',
    label: 'Episoden',
    section: 'studio',
    area: 'podcasts',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="7" y="2" width="6" height="10" rx="3" />
        <path d="M4 9v1a6 6 0 0012 0V9" />
        <path d="M10 16v3M7 19h6" />
      </svg>
    ),
  },
  {
    path: '/podcast/hosts',
    label: 'Sprecher & Besetzung',
    section: 'studio',
    area: 'podcasts',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="7" cy="7" r="2.6" />
        <circle cx="14" cy="8" r="2.2" />
        <path d="M2.5 17v-1a4.5 4.5 0 014.5-4.5h0A4.5 4.5 0 0111.5 16v1" />
        <path d="M13 11.6A4 4 0 0117.5 15.5V17" />
      </svg>
    ),
  },
  {
    path: '/hoerspiele',
    label: 'Projekte',
    section: 'studio',
    area: 'hoerspiele',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="2" y="4" width="16" height="11" rx="2" />
        <path d="M6 18h8M8.5 8.2l3.6 2.3-3.6 2.3z" />
      </svg>
    ),
  },
  {
    path: '/hoerspiele/aktivitaet',
    label: 'Aktivität',
    section: 'studio',
    area: 'hoerspiele',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 10h3l2.5-6 4 12L14 10h4" />
      </svg>
    ),
  },
  {
    path: '/voices',
    label: 'Stimmen',
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
    label: 'Stimme klonen',
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
    path: '/transcribe',
    label: 'Transkribieren',
    section: 'tools',
    icon: (
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 5h12M4 9h8M4 13h10M4 17h6" />
      </svg>
    ),
  },
  // Einstellungen sind in jedem Bereich erreichbar — vorher hingen sie am Lab
  // und der LLM-Anbieter war aus Podcasts/Hörbüchern schlicht nicht auffindbar.
  ...(['audiobooks', 'podcasts', 'hoerspiele', 'lab'] as Area[]).map((area) => ({
    path: SETTINGS_PATH[area],
    label: 'Einstellungen',
    section: 'tools' as const,
    area,
    icon: settingsIcon,
  })),
];

