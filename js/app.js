// Entry point. Registers all routes and boots the router.

import { register, init } from './router.js';
import * as LoginView     from './views/login.js';
import * as DashboardView from './views/dashboard.js';
import * as SettingsView  from './views/settings.js';

register('#/login',     LoginView);
register('#/dashboard', DashboardView);
register('#/settings',  SettingsView);

init(document.getElementById('app'));
