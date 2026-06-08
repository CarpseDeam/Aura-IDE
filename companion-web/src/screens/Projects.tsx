import { tokens, glassCard } from '../ui/theme';

function ProjectsScreen() {
  return <ComingSoon title="Projects" subtitle="Browse recent Aura projects from your phone — landing soon." icon="▤" />;
}

export function ComingSoon({ title, subtitle, icon }: { title: string; subtitle: string; icon: string }) {
  return (
    <div style={{
      padding: '2rem 1rem',
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'flex-start',
    }}>
      <div style={{ fontSize: '0.7rem', color: tokens.accent, letterSpacing: '0.2em', fontWeight: 700, marginBottom: '0.5rem' }}>
        AURA
      </div>
      <div style={{ fontSize: '1.3rem', fontWeight: 600, marginBottom: '1.25rem' }}>{title}</div>
      <div style={{
        ...glassCard,
        padding: '1.6rem 1.4rem',
        maxWidth: 380,
        width: '100%',
        textAlign: 'center',
      }}>
        <div style={{ fontSize: '1.8rem', color: tokens.accent, opacity: 0.7, marginBottom: '0.6rem' }}>
          {icon}
        </div>
        <div style={{ color: tokens.fgDim, fontSize: '0.9rem', lineHeight: 1.5 }}>
          {subtitle}
        </div>
      </div>
    </div>
  );
}

export default ProjectsScreen;
