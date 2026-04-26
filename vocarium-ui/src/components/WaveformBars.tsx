import { motion } from 'framer-motion';
import { useMemo } from 'react';

type WaveColor = 'accent' | 'aurora' | 'text' | 'success';

interface WaveformBarsProps {
  active?: boolean;
  color?: WaveColor;
  size?: 'sm' | 'md' | 'lg';
  bars?: number;
}

const sizeMap = {
  sm: { height: 16, barWidth: 2, gap: 1.5 },
  md: { height: 28, barWidth: 3, gap: 2 },
  lg: { height: 44, barWidth: 4, gap: 2.5 },
};

const colorMap: Record<WaveColor, string> = {
  accent: 'var(--color-accent)',
  aurora: 'var(--color-aurora-2)',
  text: 'var(--color-text)',
  success: 'var(--color-success)',
};

export default function WaveformBars({
  active = true,
  color = 'accent',
  size = 'md',
  bars = 16,
}: WaveformBarsProps) {
  const { height, barWidth, gap } = sizeMap[size];
  const totalWidth = bars * (barWidth + gap) - gap;

  const randoms = useMemo(
    () => Array.from({ length: bars }, () => ({
      low: 0.15 + Math.random() * 0.2,
      high: 0.5 + Math.random() * 0.5,
      dur: 0.5 + Math.random() * 0.7,
    })),
    [bars]
  );

  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'center', width: totalWidth, height }}>
      {randoms.map((r, i) => (
        <motion.div
          key={i}
          style={{
            width: barWidth,
            marginRight: i < bars - 1 ? gap : 0,
            originY: 1,
            borderRadius: '9999px',
            background: colorMap[color],
          }}
          animate={
            active
              ? { height: [height * r.low, height * r.high, height * r.low] }
              : { height: height * 0.12 }
          }
          transition={
            active
              ? { duration: r.dur, repeat: Infinity, ease: 'easeInOut', delay: i * 0.04 }
              : { duration: 0.3 }
          }
        />
      ))}
    </div>
  );
}
