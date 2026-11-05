# Conformance capture proxy: static binary on distroless nonroot (spec 15.2).
FROM golang:1.24.7-bookworm AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
ENV CGO_ENABLED=0 GOOS=linux GOTOOLCHAIN=local
RUN go build -trimpath -ldflags="-s -w" -o /out/capture ./cmd/capture

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/capture /capture
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/capture"]
