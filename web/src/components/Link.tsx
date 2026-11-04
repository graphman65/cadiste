import type { SessionInfo } from "../types";
import { Icon } from "./Icon";

/**
 * Harness sessions asking to be linked to a tab, one card each over the app; whichever tab links one first takes it,
 * and the card leaves every tab as the session stops asking.
 */
export function LinkRequests({
  sessions,
  onLink,
  onDecline,
}: {
  sessions: SessionInfo[];
  onLink: (session: string) => void;
  onDecline: (session: string) => void;
}) {
  const asking = sessions.filter((session) => session.requesting && session.tab === null);
  if (asking.length === 0) return null;
  return (
    <div className="link-requests" role="dialog" aria-label="Link requests">
      {asking.map((session) => (
        <div key={session.id} className="link-card">
          <div className="link-title">
            <Icon name="links" />
            <span>
              <strong>{session.client}</strong> wants to link to a cadiste tab
            </span>
          </div>
          <code className="link-code">{session.code}</code>
          <div className="link-actions">
            <button onClick={() => onDecline(session.id)}>Dismiss</button>
            <button className="primary" onClick={() => onLink(session.id)} autoFocus>
              Link this tab
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

/** The sessions linked to this tab, in the top bar, each with a button to unlink it. */
export function LinkedSessions({
  sessions,
  tab,
  onUnlink,
}: {
  sessions: SessionInfo[];
  tab: string | null;
  onUnlink: (session: string) => void;
}) {
  return sessions
    .filter((session) => tab !== null && session.tab === tab)
    .map((session) => (
      <span key={session.id} className="link-badge" title={`Linked to ${session.client} (${session.code})`}>
        <Icon name="links" />
        {session.client} · <code>{session.code}</code>
        <button className="icon-button" onClick={() => onUnlink(session.id)} title="Unlink">
          <Icon name="close" />
        </button>
      </span>
    ));
}
