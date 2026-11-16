FROM nginx:1.27-alpine
# The official image renders /etc/nginx/templates/*.template with envsubst at start.
# Limit substitution to the one variable we want, so nginx's own $variables survive.
ENV NGINX_ENVSUBST_FILTER=OPENROUTER_API_KEY
COPY default.conf.template /etc/nginx/templates/default.conf.template
