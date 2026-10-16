/** Presentation only: this marker never participates in authentication or API access. */
export const isAndroidApp = typeof window !== 'undefined' && (
  /StudyToolAndroid\//.test(navigator.userAgent)
  || new URLSearchParams(window.location.search).get('client') === 'android'
);

export function initializeClientLayout(): void {
  if (isAndroidApp) document.documentElement.dataset.client = 'android';
}
