import { useState, useEffect, useCallback, useRef } from 'react';

// Polling hook that's safe against inline fetchFn callers.
// Previously, depending on `fetchFn` in useCallback meant any caller passing
// an inline arrow function (the common case) would recreate `refresh` every
// render, retrigger the useEffect, and burn CPU in a near-infinite loop.
// We now stash the latest fetchFn in a ref so the polling effect depends only
// on intervalMs.
export function usePolling(fetchFn, intervalMs = 5000) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchRef = useRef(fetchFn);
  useEffect(() => {
    fetchRef.current = fetchFn;
  }, [fetchFn]);

  const refresh = useCallback(async () => {
    try {
      const result = await fetchRef.current();
      setData(result);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, intervalMs);
    return () => clearInterval(id);
  }, [refresh, intervalMs]);

  return { data, error, loading, refresh };
}
