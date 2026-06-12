import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import CompanionSocket, { socket } from '../api/socket';
import { useDesktopVerification } from '../hooks/useDesktopVerification';
import { tokens, glassCard, statusPillStyle } from '../ui/theme';

interface CompanionProject {
  id: string;
  name: string;
  updated_at: string;
  thread_count: number;
}

interface CompanionThread {
  id: string;
  title: string;
  updated_at: string;
  is_current: boolean;
}

function ProjectsScreen() {
  const navigate = useNavigate();
  const isPaired = CompanionSocket.isPaired();
  const { phase, error: verifyError, retry, goToLogin } = useDesktopVerification();

  const [projects, setProjects] = useState<CompanionProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [threads, setThreads] = useState<CompanionThread[]>([]);
  const [loadingThreads, setLoadingThreads] = useState(false);
  const [projectError, setProjectError] = useState('');
  const [selectingThreadId, setSelectingThreadId] = useState<string | null>(null);
  const [threadError, setThreadError] = useState('');

  // Early redirect for unpaired / missing desktop
  useEffect(() => {
    if (!isPaired) {
      navigate('/login', { replace: true });
      return;
    }
    const desktopId =
      sessionStorage.getItem('companion_desktop_id') ||
      CompanionSocket.getStoredSafeContext().desktop_id ||
      '';
    if (!desktopId) {
      navigate('/login', { replace: true });
      return;
    }
  }, [isPaired, navigate]);

  // Phase-gated project fetch — only send project.list_recent after verified
  useEffect(() => {
    if (phase !== 'connected') return;

    setLoading(true);
    setProjectError('');

    const desktopId =
      sessionStorage.getItem('companion_desktop_id') ||
      CompanionSocket.getStoredSafeContext().desktop_id ||
      '';
    if (!desktopId) return;

    // 1. Register listeners
    const unsubProjectList = socket.on('project.list_result', (msg: any) => {
      if (msg.payload?.error) {
        setProjectError(msg.payload.error);
        setLoading(false);
        return;
      }
      const list: CompanionProject[] | null = msg.payload?.projects;
      setProjects(list ?? []);
      setLoading(false);
    });

    const unsubConversationList = socket.on(
      'conversation.list_result',
      (msg: any) => {
        if (msg.payload?.error) {
          setThreadError(msg.payload.error);
          setLoadingThreads(false);
          return;
        }
        const list: CompanionThread[] | null = msg.payload?.threads;
        setThreads(list ?? []);
        setLoadingThreads(false);
      }
    );

    const unsubConversationSelected = socket.on(
      'conversation.selected',
      (msg: any) => {
        const payload = msg.payload || {};
        if (payload.error) {
          setProjectError(payload.error);
          setSelectingThreadId(null);
          return;
        }
        setSelectingThreadId(null);
        const safeCtx = {
          ...CompanionSocket.getStoredSafeContext(),
          project_id: payload.project_id,
          conversation_id: payload.thread_id,
        };
        CompanionSocket.setStoredSafeContext(safeCtx);
        navigate('/chat');
      }
    );

    // 2. Then send the request
    socket.send('project.list_recent', {}, desktopId);

    return () => {
      unsubProjectList();
      unsubConversationList();
      unsubConversationSelected();
    };
  }, [phase, navigate]);

  function handleSelectProject(project: CompanionProject) {
    if (selectedProjectId === project.id) {
      setSelectedProjectId(null);
      setThreads([]);
      return;
    }
    setSelectedProjectId(project.id);
    setThreads([]);
    setLoadingThreads(true);
    setProjectError('');
    setThreadError('');

    const desktopId =
      sessionStorage.getItem('companion_desktop_id') ||
      CompanionSocket.getStoredSafeContext().desktop_id ||
      '';
    socket.send('conversation.list', { project_id: project.id }, desktopId);
  }

  function handleSelectThread(thread: CompanionThread) {
    if (selectingThreadId) return;

    const desktopId =
      sessionStorage.getItem('companion_desktop_id') ||
      CompanionSocket.getStoredSafeContext().desktop_id ||
      '';
    if (!selectedProjectId || !desktopId) return;

    setProjectError('');
    setSelectingThreadId(thread.id);
    socket.send(
      'conversation.select',
      { project_id: selectedProjectId, thread_id: thread.id },
      desktopId
    );
  }

  function formatDate(dateStr: string): string {
    if (!dateStr) return '';
    try {
      const d = new Date(dateStr);
      const now = Date.now();
      const diff = now - d.getTime();
      if (diff < 60000) return 'just now';
      if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
      if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
      return d.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return dateStr;
    }
  }

  // Connecting / verifying full-screen spinner
  if (phase === 'connecting' || phase === 'verifying') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh', padding: '0 0.75rem', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ ...glassCard, padding: '2rem 1.5rem', textAlign: 'center', maxWidth: 380 }}>
          <div style={{
            width: 36, height: 36, borderRadius: '50%',
            border: `3px solid ${tokens.border}`,
            borderTopColor: tokens.accent,
            animation: 'spin 0.9s linear infinite',
            margin: '0 auto 1rem',
          }} />
          <div style={{ color: tokens.fgDim, fontSize: '0.9rem' }}>
            {phase === 'connecting' ? 'Connecting to your Aura desktop…' : 'Verifying with your Aura desktop…'}
          </div>
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      </div>
    );
  }

  // Unavailable full-screen card
  if (phase === 'unavailable') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh', padding: '0 0.75rem', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ ...glassCard, padding: '1.5rem', textAlign: 'center', maxWidth: 380, width: '100%' }}>
          <div style={{ fontSize: '1.1rem', fontWeight: 600, color: tokens.danger, marginBottom: 4 }}>
            Previous desktop unavailable
          </div>
          <div style={{ color: tokens.fgDim, fontSize: '0.9rem', marginBottom: '1rem' }}>
            {verifyError || 'Could not reach your Aura desktop.'}
          </div>
          <button
            onClick={goToLogin}
            style={{
              width: '100%', padding: '0.75rem 1rem',
              background: tokens.accent, color: '#0a0f1f',
              border: 'none', borderRadius: 10,
              fontSize: '0.9rem', fontWeight: 600,
              marginBottom: '0.5rem', cursor: 'pointer',
            }}
          >
            Go to Login
          </button>
          <button
            onClick={retry}
            style={{
              width: '100%', padding: '0.75rem 1rem',
              background: 'transparent', color: tokens.fg,
              border: `1px solid ${tokens.borderStrong}`,
              borderRadius: 10, fontSize: '0.9rem', fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  // Connected — normal project list UI
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100dvh',
        padding: '0 0.75rem',
      }}
    >
      {/* Header */}
      <header
        style={{
          ...glassCard,
          margin: '0.75rem 0 0.5rem',
          padding: '0.75rem 1rem',
          display: 'flex',
          alignItems: 'center',
          gap: '0.75rem',
        }}
      >
        <button
          onClick={() => navigate(-1)}
          aria-label="Back"
          style={{
            background: 'transparent',
            border: 'none',
            color: tokens.fgDim,
            fontSize: '1.4rem',
            padding: '0.1rem 0.4rem',
          }}
        >
          ←
        </button>
        <div style={{ flex: 1, fontWeight: 600, fontSize: '0.95rem' }}>
          Projects
        </div>
        <span style={statusPillStyle('connected')}>
          ● Online
        </span>
      </header>

      {/* Error banner */}
      {projectError && (
        <div
          style={{
            padding: '0.55rem 0.85rem',
            background: 'rgba(247,118,142,0.08)',
            border: `1px solid ${tokens.danger}`,
            color: tokens.danger,
            borderRadius: 10,
            fontSize: '0.85rem',
            marginBottom: '0.5rem',
          }}
        >
          {projectError}
        </div>
      )}

      {/* Main scrollable area */}
      <main style={{ flex: 1, overflow: 'auto', padding: '0.25rem 0 1rem' }}>
        {/* Projects list */}
        {loading && (
          <div
            style={{
              textAlign: 'center',
              color: tokens.fgMuted,
              padding: '2rem 1rem',
              fontSize: '0.9rem',
            }}
          >
            Loading…
          </div>
        )}

        {!loading && projectError && projects.length === 0 && (
          <div style={{ textAlign: 'center', padding: '1rem' }}>
            <button
              onClick={() => {
                setProjectError('');
                setLoading(true);
                const desktopId =
                  sessionStorage.getItem('companion_desktop_id') ||
                  CompanionSocket.getStoredSafeContext().desktop_id ||
                  '';
                if (desktopId) {
                  socket.send('project.list_recent', {}, desktopId);
                }
              }}
              style={{
                padding: '0.5rem 1rem',
                background: 'transparent',
                color: tokens.fg,
                border: `1px solid ${tokens.borderStrong}`,
                borderRadius: 10,
                fontSize: '0.85rem',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              Retry
            </button>
          </div>
        )}

        {!loading && !projectError && projects.length === 0 && (
          <div
            style={{
              textAlign: 'center',
              marginTop: '2rem',
              color: tokens.fgMuted,
              padding: '1rem',
            }}
          >
            <div
              style={{
                fontSize: '2rem',
                color: tokens.accent,
                opacity: 0.45,
                marginBottom: '0.5rem',
              }}
            >
              ▤
            </div>
            <div style={{ fontSize: '0.95rem', color: tokens.fgDim }}>
              No Aura projects yet.
            </div>
            <div style={{ fontSize: '0.8rem', marginTop: 6 }}>
              Open or create a project on desktop.
            </div>
          </div>
        )}

        {!loading &&
          projects.map((project) => (
            <div key={project.id}>
              {/* Project card */}
              <div
                onClick={() => handleSelectProject(project)}
                style={{
                  ...glassCard,
                  padding: '0.85rem 1rem',
                  marginBottom: '0.5rem',
                  minHeight: 60,
                  cursor: 'pointer',
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'center',
                  borderColor:
                    selectedProjectId === project.id
                      ? tokens.accent
                      : tokens.border,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'flex-start',
                  }}
                >
                  <div style={{ fontWeight: 600, fontSize: '0.95rem' }}>
                    {project.name}
                  </div>
                  <div
                    style={{
                      fontSize: '0.72rem',
                      color: tokens.fgMuted,
                      whiteSpace: 'nowrap',
                      marginLeft: '0.5rem',
                    }}
                  >
                    {formatDate(project.updated_at)}
                  </div>
                </div>
                <div
                  style={{
                    fontSize: '0.78rem',
                    color: tokens.fgDim,
                    marginTop: 4,
                  }}
                >
                  {project.thread_count} thread
                  {project.thread_count !== 1 ? 's' : ''}
                </div>
              </div>

              {/* Threads section (visible when this project is selected) */}
              {selectedProjectId === project.id && (
                <div
                  style={{
                    marginLeft: '0.75rem',
                    borderLeft: `2px solid ${tokens.border}`,
                    paddingLeft: '0.75rem',
                    marginBottom: '0.5rem',
                  }}
                >
                  {loadingThreads && (
                    <div
                      style={{
                        padding: '0.75rem 0.5rem',
                        color: tokens.fgMuted,
                        fontSize: '0.85rem',
                      }}
                    >
                      Loading…
                    </div>
                  )}

                  {!loadingThreads && threads.length === 0 && !threadError && (
                    <div
                      style={{
                        padding: '0.75rem 0.5rem',
                        color: tokens.fgMuted,
                        fontSize: '0.85rem',
                      }}
                    >
                      No conversations in this project yet. Start one on
                      desktop, then refresh.
                    </div>
                  )}

                  {!loadingThreads && threadError && (
                    <div
                      style={{
                        padding: '0.55rem 0.85rem',
                        background: 'rgba(247,118,142,0.08)',
                        border: `1px solid ${tokens.danger}`,
                        color: tokens.danger,
                        borderRadius: 10,
                        fontSize: '0.85rem',
                        marginBottom: '0.5rem',
                      }}
                    >
                      {threadError}
                      <button
                        onClick={() => {
                          setThreadError('');
                          setLoadingThreads(true);
                          const desktopId =
                            sessionStorage.getItem('companion_desktop_id') ||
                            CompanionSocket.getStoredSafeContext().desktop_id ||
                            '';
                          socket.send('conversation.list', { project_id: selectedProjectId! }, desktopId);
                        }}
                        style={{
                          marginLeft: '0.5rem',
                          padding: '0.2rem 0.6rem',
                          background: 'transparent',
                          color: tokens.fg,
                          border: `1px solid ${tokens.borderStrong}`,
                          borderRadius: 6,
                          fontSize: '0.8rem',
                          cursor: 'pointer',
                        }}
                      >
                        Retry
                      </button>
                    </div>
                  )}

                  {!loadingThreads &&
                    threads.map((thread) => {
                      const isSelecting = selectingThreadId === thread.id;
                      return (
                      <div
                        key={thread.id}
                        onClick={() => handleSelectThread(thread)}
                        style={{
                          ...glassCard,
                          padding: '0.7rem 0.9rem',
                          marginBottom: '0.4rem',
                          cursor: isSelecting ? 'default' : 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          gap: '0.6rem',
                          opacity: isSelecting ? 0.55 : 1,
                        }}
                      >
                        {isSelecting ? (
                          <div style={{ flex: 1, fontSize: '0.85rem', color: tokens.fgMuted }}>
                            Selecting…
                          </div>
                        ) : (
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div
                            style={{
                              fontSize: '0.9rem',
                              fontWeight: 500,
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                            }}
                          >
                            {thread.title || 'Untitled'}
                          </div>
                          <div
                            style={{
                              fontSize: '0.72rem',
                              color: tokens.fgMuted,
                              marginTop: 2,
                            }}
                          >
                            {formatDate(thread.updated_at)}
                          </div>
                        </div>
                        )}
                        {thread.is_current && (
                          <span
                            style={{
                              width: 8,
                              height: 8,
                              borderRadius: '50%',
                              background: tokens.success,
                              flexShrink: 0,
                            }}
                            title="Current conversation"
                          />
                        )}
                      </div>
                      );
                    })}
                </div>
              )}
            </div>
          ))}
      </main>
    </div>
  );
}

export function ComingSoon({
  title,
  subtitle,
  icon,
}: {
  title: string;
  subtitle: string;
  icon: string;
}) {
  return (
    <div
      style={{
        padding: '2rem 1rem',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'flex-start',
      }}
    >
      <div
        style={{
          fontSize: '0.7rem',
          color: tokens.accent,
          letterSpacing: '0.2em',
          fontWeight: 700,
          marginBottom: '0.5rem',
        }}
      >
        AURA
      </div>
      <div style={{ fontSize: '1.3rem', fontWeight: 600, marginBottom: '1.25rem' }}>
        {title}
      </div>
      <div
        style={{
          ...glassCard,
          padding: '1.6rem 1.4rem',
          maxWidth: 380,
          width: '100%',
          textAlign: 'center',
        }}
      >
        <div
          style={{
            fontSize: '1.8rem',
            color: tokens.accent,
            opacity: 0.7,
            marginBottom: '0.6rem',
          }}
        >
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
