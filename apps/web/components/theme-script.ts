/**
 * Shared between the server-rendered <head> and the client store, so the
 * storage key and the attribute contract cannot drift apart.
 *
 * The CSS in globals.css treats a *missing* data-theme as "follow the system",
 * which is why this script writes the attribute only when an explicit choice
 * has been stored. Nothing here runs on the server; the string is inlined into
 * the document head and executes before first paint, so a viewer who chose the
 * theme opposite to their OS never sees a flash of the wrong one.
 */

export const THEME_STORAGE_KEY = "jasmine-theme";

/** Fires on the window when the stored preference changes in this tab. */
export const THEME_CHANGE_EVENT = "jasmine:themechange";

export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)});if(t==="light"||t==="dark"){document.documentElement.setAttribute("data-theme",t)}}catch(e){}})();`;
