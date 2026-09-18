import type { Voice } from '../types';
import { groupEngineVoices } from '../voiceUtils';

interface Props {
  voices: Voice[];
  /** Optional per-voice label, e.g. to append cache-readiness marks. */
  label?: (voice: Voice) => string;
}

/**
 * `<option>` list for every picker that selects a speaking voice.
 *
 * The grouping is the point: OmniVoice is the engine to use, and the Kikiri
 * bank underneath it only exists for the case where OmniVoice cannot run.
 * A flat alphabetical list made the 44 CPU voices look like 44 equally good
 * choices and buried the handful of real clone voices among them.
 */
export function VoiceOptions({ voices, label }: Props) {
  return (
    <>
      {groupEngineVoices(voices).map((group) => (
        <optgroup
          key={group.label}
          label={group.hint ? `${group.label} (${group.hint})` : group.label}
        >
          {group.voices.map((v) => (
            <option key={v.id} value={v.id}>
              {label ? label(v) : v.name}
            </option>
          ))}
        </optgroup>
      ))}
    </>
  );
}
