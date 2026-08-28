export default function Card({ children, className = "", ...props }) {
  return (
    <div
      className={`rounded-xl border border-ink-700 bg-ink-900/60 backdrop-blur-sm ${className}`}
      {...props}
    >
      {children}
    </div>
  );
}
