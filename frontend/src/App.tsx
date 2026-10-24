import { useEffect, useState } from "react";
import Library from "./pages/Library";
import Reader from "./pages/Reader";

/** Tiny hash router: "#/" is the library, "#/read/<book id>" is the reader. */
function useRoute(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

export default function App() {
  const route = useRoute();
  const match = route.match(/^#\/read\/([a-z0-9-]+)$/);
  return match ? <Reader key={match[1]} bookId={match[1]} /> : <Library />;
}
