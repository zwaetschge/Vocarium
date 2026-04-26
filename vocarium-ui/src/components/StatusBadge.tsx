import { motion } from 'framer-motion';

interface StatusBadgeProps {
  status: 'online' | 'offline' | 'loading';
  label?: string;
}

const config = {
  online: { dot: 'var(--color-success)', bg: 'var(--color-success-dim)', text: 'var(--color-success)', label: 'Online' },
  offline: { dot: 'var(--color-danger)', bg: 'var(--color-danger-dim)', text: 'var(--color-danger)', label: 'Offline' },
  loading: { dot: 'var(--color-warning)', bg: 'var(--color-warning-dim)', text: 'var(--color-warning)', label: 'Connecting...' },
};

export default function StatusBadge({ status, label }: StatusBadgeProps) {
  const c = config[status];
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '4px 10px',
        fontSize: '12px',
        fontWeight: 500,
        borderRadius: '9999px',
        background: c.bg,
        color: c.text,
      }}
    >
      <motion.span
        style={{
          display: 'block',
          width: '6px',
          height: '6px',
          borderRadius: '50%',
          background: c.dot,
        }}
        animate={
          status === 'loading'
            ? { opacity: [1, 0.3, 1] }
            : status === 'online'
              ? { scale: [1, 1.4, 1] }
              : {}
        }
        transition={{ duration: 1.5, repeat: Infinity }}
      />
      {label || c.label}
    </motion.div>
  );
}
