FROM --platform=$BUILDPLATFORM golang:1.24.7-bookworm AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /out/budget-backend ./cmd/budget-backend
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/budget-backend /budget-backend
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/budget-backend"]
