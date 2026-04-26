import { motion } from 'framer-motion';

export default function SkeletonCard() {
  return (
    <motion.div
      className="card-subtle"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.3 }}
      style={{ padding: '20px' }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: '18px', gap: '12px' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="skeleton-shimmer" style={{ height: '18px', width: '62%', borderRadius: '6px', marginBottom: '10px' }} />
          <div className="skeleton-shimmer" style={{ height: '11px', width: '35%', borderRadius: '4px' }} />
        </div>
        <div className="skeleton-shimmer" style={{ width: '36px', height: '36px', borderRadius: '50%', flexShrink: 0 }} />
      </div>
      <div style={{ display: 'flex', gap: '8px' }}>
        <div className="skeleton-shimmer" style={{ height: '22px', width: '70px', borderRadius: '999px' }} />
        <div className="skeleton-shimmer" style={{ height: '22px', width: '54px', borderRadius: '999px' }} />
      </div>
    </motion.div>
  );
}
