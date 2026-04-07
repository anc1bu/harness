// Entry point. Registers all routes and boots the router.

import { register, init } from './router.js';
import { restoreSession }  from './auth.js';
import * as LoginView     from './views/login.js';
import * as DashboardView from './views/dashboard.js';
import * as SettingsView  from './views/settings.js';
import * as AdminView     from './views/admin.js';

restoreSession();

register('#/login',     LoginView);
register('#/dashboard', DashboardView);
register('#/settings',  SettingsView);
register('#/admin',     AdminView);

init(document.getElementById('app'));
