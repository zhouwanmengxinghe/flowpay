// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { sessionApi } from "@/lib/session-api";
import type { TraceEvent } from "@/state";
import { useSessionEvents } from "./use-session-events";

class Stream extends EventTarget {
  static instances: Stream[] = [];
  onopen = null;
  onerror = null;
  closed = false;
  constructor(public url: string) {
    super();
    Stream.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  push(seq: number, type = "model.delta") {
    const event: TraceEvent = {
      schema_version: 1,
      event_id: `event_${seq}`,
      session_id: "fixture",
      seq,
      type,
      run_id: "run",
      call_id: "call",
      parent_call_id: null,
      timestamp: "2026-10-17T00:00:00Z",
      payload: { content: `${seq}`, status: "completed" },
    };
    this.dispatchEvent(
      new MessageEvent("trace", { data: JSON.stringify(event) }),
    );
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  Stream.instances = [];
  vi.stubGlobal("EventSource", Stream);
  vi.spyOn(sessionApi, "getEvents").mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function mounted() {
  const refresh = vi.fn(async () => {});
  const report = vi.fn();
  const hook = renderHook(
    ({ id }) => useSessionEvents(id, 0, refresh, report),
    {
      initialProps: { id: "one" },
    },
  );
  await act(async () => {
    await Promise.resolve();
  });
  return { ...hook, refresh };
}

it("merges a burst of 1000 fragments in one scheduled update without losing order", async () => {
  const { result } = await mounted();
  act(() => {
    for (let seq = 1; seq <= 1000; seq++) Stream.instances[0].push(seq);
  });
  expect(result.current.events).toHaveLength(0);
  act(() => {
    vi.advanceTimersByTime(50);
  });
  expect(result.current.events.map((event) => event.seq)).toEqual(
    Array.from({ length: 1000 }, (_, index) => index + 1),
  );
});

it("flushes preceding fragments immediately when a run finishes", async () => {
  const { result } = await mounted();
  act(() => {
    Stream.instances[0].push(1);
  });
  act(() => {
    Stream.instances[0].push(2, "run.finished");
  });
  expect(result.current.events.map((event) => event.seq)).toEqual([1, 2]);
  expect(result.current.status).toBe("completed");
});

it("discards pending fragments when switching sessions", async () => {
  const { result, rerender } = await mounted();
  const old = Stream.instances[0];
  act(() => {
    old.push(1);
  });
  await act(async () => {
    rerender({ id: "two" });
    await Promise.resolve();
  });
  act(() => {
    Stream.instances.at(-1)!.push(2);
    vi.advanceTimersByTime(50);
  });
  expect(old.closed).toBe(true);
  expect(result.current.events.map((event) => event.seq)).toEqual([2]);
});

it("does not restore buffered events after a session is deleted", async () => {
  const { result, refresh } = await mounted();
  act(() => {
    Stream.instances[0].push(1);
    Stream.instances[0].dispatchEvent(
      new MessageEvent("session-deleted", { data: "{}" }),
    );
    vi.advanceTimersByTime(50);
  });
  expect(result.current.events).toHaveLength(0);
  expect(Stream.instances[0].closed).toBe(true);
  expect(refresh).toHaveBeenCalledTimes(1);
});
