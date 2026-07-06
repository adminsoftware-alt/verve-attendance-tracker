const { createProxyMiddleware } = require('http-proxy-middleware');

module.exports = function(app) {
  // Add OWASP security headers to all responses
  app.use((req, res, next) => {
    res.setHeader('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Content-Security-Policy', "default-src 'self' https://*.zoom.us https://*.zoomgov.com; script-src 'self' 'unsafe-inline' 'unsafe-eval' https://*.zoom.us; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' https://*.zoom.us https://*.zoomgov.com wss://*.zoom.us; frame-ancestors https://*.zoom.us https://*.zoomgov.com");
    res.setHeader('X-Frame-Options', 'ALLOW-FROM https://zoom.us');
    res.setHeader('X-XSS-Protection', '1; mode=block');
    next();
  });

  // Proxy API calls to backend server (Flask app.py on :8080, same as apiService.js)
  app.use(
    '/calibration',
    createProxyMiddleware({
      target: 'http://localhost:8080',
      changeOrigin: true,
    })
  );

  app.use(
    '/monitor',
    createProxyMiddleware({
      target: 'http://localhost:8080',
      changeOrigin: true,
    })
  );

  app.use(
    '/health',
    createProxyMiddleware({
      target: 'http://localhost:3001',
      changeOrigin: true,
    })
  );

  app.use(
    '/auth',
    createProxyMiddleware({
      target: 'http://localhost:3001',
      changeOrigin: true,
    })
  );
};
