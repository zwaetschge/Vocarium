import { useEffect, useState } from 'react';

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

const DISMISS_KEY = 'vocarium-install-dismissed-at';
const SNOOZE_MS = 7 * 24 * 3600 * 1000;

/** PWA-Installationskarte nach dem Canto-Muster: erscheint erst nach dem
 *  Onboarding, „Später" schweigt 7 Tage, verschwindet nach Installation. */
export default function InstallPrompt() {
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const onPrompt = (e: Event) => {
      e.preventDefault();
      const dismissedAt = Number(localStorage.getItem(DISMISS_KEY) || 0);
      if (Date.now() - dismissedAt < SNOOZE_MS) return;
      if (!localStorage.getItem('ab-onboarding-done')) return;
      setDeferred(e as BeforeInstallPromptEvent);
      setVisible(true);
    };
    const onInstalled = () => setVisible(false);
    window.addEventListener('beforeinstallprompt', onPrompt);
    window.addEventListener('appinstalled', onInstalled);
    return () => {
      window.removeEventListener('beforeinstallprompt', onPrompt);
      window.removeEventListener('appinstalled', onInstalled);
    };
  }, []);

  if (!visible || !deferred) return null;

  return (
    <div style={{
      position: 'fixed', bottom: '18px', left: '18px', zIndex: 80, width: '280px',
      padding: '16px 18px', borderRadius: '14px',
      background: 'rgba(20,20,26,0.92)', backdropFilter: 'blur(14px)',
      border: '1px solid rgba(255,255,255,0.12)', boxShadow: '0 12px 40px rgba(0,0,0,0.45)',
    }}>
      <strong style={{ display: 'block', fontSize: '14px', marginBottom: '4px' }}>Vocarium installieren</strong>
      <p style={{ margin: '0 0 12px', fontSize: '12px', opacity: 0.7, lineHeight: 1.5 }}>
        Schneller Zugriff vom Homescreen, Hörbücher offline nutzbar.
      </p>
      <div style={{ display: 'flex', gap: '8px' }}>
        <button className="btn btn-primary btn-sm" style={{ flex: 1 }}
          onClick={async () => {
            await deferred.prompt();
            setVisible(false);
          }}>Installieren</button>
        <button className="btn btn-ghost btn-sm"
          onClick={() => {
            localStorage.setItem(DISMISS_KEY, String(Date.now()));
            setVisible(false);
          }}>Später</button>
      </div>
    </div>
  );
}
