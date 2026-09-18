import { createContext, useContext, useEffect, useState, type ReactNode, type SetStateAction } from 'react';

type Drafts = Record<string, string>;
const DraftContext = createContext<{ drafts: Drafts; set: (key: string, value: string | undefined) => void }>({ drafts: {}, set: () => {} });

/** In-memory drafts live above routes; never leak edited content into shared local storage. */
export function EditorDraftsProvider({ children }: { children: ReactNode }) {
  const [drafts, setDrafts] = useState<Drafts>({});
  useEffect(() => {
    if (!Object.keys(drafts).length) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [drafts]);
  return <DraftContext.Provider value={{ drafts, set: (key, value) => setDrafts(prev => {
    const next = { ...prev };
    if (value === undefined) delete next[key]; else next[key] = value;
    return next;
  }) }}>{children}</DraftContext.Provider>;
}

export function useDraftField(key: string, saved: string): [string, (value: SetStateAction<string>) => void, () => void] {
  const { drafts, set } = useContext(DraftContext);
  const value = drafts[key] ?? saved;
  return [value, next => {
    const resolved = typeof next === 'function' ? next(value) : next;
    set(key, resolved === saved ? undefined : resolved);
  }, () => set(key, undefined)];
}

export function useHasDrafts(prefix: string): boolean {
  const { drafts } = useContext(DraftContext);
  return Object.keys(drafts).some(key=>key.startsWith(prefix));
}

export function useEditorDraftActions() { return useContext(DraftContext).set; }
