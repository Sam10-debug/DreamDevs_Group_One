# Use lightweight web server image
FROM nginx:alpine

# Copy files to NGINX default directory
COPY . /usr/share/nginx/html

# Expose port 80 (NGINX default)
EXPOSE 80

# Start NGINX
CMD ["nginx", "-g", "daemon off;"]