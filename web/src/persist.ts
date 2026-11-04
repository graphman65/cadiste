import { useCallback, useState } from "react";

/** A number kept in localStorage, so panel sizes survive a reload. */
export function usePersistentNumber(key: string, initial: number): [number, (value: number) => void] {
  const [value, setValue] = useState(() => {
    const stored = Number(window.localStorage.getItem(key));
    return Number.isFinite(stored) && stored > 0 ? stored : initial;
  });
  const update = useCallback(
    (next: number) => {
      setValue(next);
      window.localStorage.setItem(key, String(next));
    },
    [key],
  );
  return [value, update];
}

/** A flag kept in localStorage, so a panel stays hidden or shown across reloads. */
export function usePersistentFlag(key: string, initial: boolean): [boolean, (value: boolean) => void] {
  const [value, setValue] = useState(() => {
    const stored = window.localStorage.getItem(key);
    return stored === null ? initial : stored === "true";
  });
  const update = useCallback(
    (next: boolean) => {
      setValue(next);
      window.localStorage.setItem(key, String(next));
    },
    [key],
  );
  return [value, update];
}

/** One of `choices` kept in localStorage (a chosen tab); the first if what is stored is not one of them. */
export function usePersistentChoice<T extends string>(key: string, choices: readonly T[]): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => choices.find((each) => each === window.localStorage.getItem(key)) ?? choices[0]);
  const update = useCallback(
    (next: T) => {
      setValue(next);
      window.localStorage.setItem(key, next);
    },
    [key],
  );
  return [value, update];
}

/** A set of strings kept in localStorage (closed folders, shown detail sections), with a toggle. */
export function usePersistentSet(key: string): [ReadonlySet<string>, (item: string) => void] {
  const [items, setItems] = useState<ReadonlySet<string>>(() => {
    try {
      const stored: unknown = JSON.parse(window.localStorage.getItem(key) ?? "[]");
      return new Set(Array.isArray(stored) ? stored.filter((item): item is string => typeof item === "string") : []);
    } catch {
      return new Set();
    }
  });
  const toggle = useCallback(
    (item: string) => {
      setItems((current) => {
        const next = new Set(current);
        if (next.has(item)) next.delete(item);
        else next.add(item);
        window.localStorage.setItem(key, JSON.stringify([...next]));
        return next;
      });
    },
    [key],
  );
  return [items, toggle];
}
