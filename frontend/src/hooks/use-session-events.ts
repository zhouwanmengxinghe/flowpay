import { useEffect, useState } from "react";
import { errorMessage, sessionApi } from "@/lib/session-api";
import { mergeEvents, statusOf, type TraceEvent } from "@/state";

export type ConnectionStatus =
  "connecting" | "connected" | "reconnecting" | "error";

/** 先读取历史，再按游标订阅；切换会话仅断开 SSE，不停止后端 Run。 */
export function useSessionEvents(
  sessionId: string | null,
  retry: number,
  refreshSessions: () => Promise<void>,
  reportError: (message: string) => void,
) {
  const [snapshot, setSnapshot] = useState<{
    sessionId: string | null;
    events: TraceEvent[];
  }>({
    sessionId: null,
    events: [],
  });
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");
  useEffect(() => {
    setSnapshot({ sessionId, events: [] });
    setConnection("connecting");
    if (!sessionId) return;
    let disposed = false;
    let stream: EventSource | undefined;
    let pending: TraceEvent[] = [];
    let flushTimer: ReturnType<typeof setTimeout> | undefined;
    const flush = () => {
      if (flushTimer !== undefined) clearTimeout(flushTimer);
      flushTimer = undefined;
      if (disposed || !pending.length) return;
      const batch = pending;
      pending = [];
      setSnapshot((previous) => ({
        sessionId,
        events: mergeEvents(previous.events, batch),
      }));
    };
    sessionApi
      .getEvents(sessionId)
      .then((history) => {
        if (disposed) return;
        setSnapshot({ sessionId, events: history });
        stream = new EventSource(
          `/api/sessions/${sessionId}/stream?after=${history.at(-1)?.seq ?? 0}`,
        );
        stream.onopen = () => {
          if (!disposed) setConnection("connected");
        };
        stream.onerror = () => {
          if (!disposed) {
            setConnection("reconnecting");
            // 会话可能在历史读取之后、SSE 建立之前被其他页面删除。
            refreshSessions().catch(() => {
              /* 网络故障时保留重连状态。 */
            });
          }
        };
        stream.addEventListener("session-deleted", () => {
          if (disposed) return;
          disposed = true;
          stream?.close();
          if (flushTimer !== undefined) clearTimeout(flushTimer);
          pending = [];
          setSnapshot({ sessionId, events: [] });
          refreshSessions().catch((error) => reportError(errorMessage(error)));
        });
        stream.addEventListener("trace", (event) => {
          if (disposed) return;
          const next = JSON.parse((event as MessageEvent).data) as TraceEvent;
          // 分片可能连续到达；合并后再渲染，避免每个原始帧都触发 Markdown 和轨迹计算。
          pending.push(next);
          if (next.type.startsWith("run.")) flush();
          else if (flushTimer === undefined) flushTimer = setTimeout(flush, 50);
          if (next.type.startsWith("run.") || next.type === "message.user")
            refreshSessions().catch((error) => {
              if (!disposed) reportError(errorMessage(error));
            });
        });
      })
      .catch((error) => {
        if (!disposed) {
          setConnection("error");
          reportError(errorMessage(error));
        }
      });
    return () => {
      disposed = true;
      if (flushTimer !== undefined) clearTimeout(flushTimer);
      pending = [];
      stream?.close();
    };
  }, [sessionId, retry, refreshSessions, reportError]);
  // effect 清理前也不让旧会话事件进入新会话的渲染。
  const events = snapshot.sessionId === sessionId ? snapshot.events : [];
  return { events, connection, status: statusOf(events) };
}
